"""Replay fixed BTCUSDT windows from Binance USD-M monthly ZIPs without a database."""

import argparse
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.backtest_engine import CANDLE_COLUMNS, STRATEGIES, replay, validate_candles


FEE_BPS = 5
SLIPPAGE_BPS = 2
INITIAL = 10_000
DATA_START = pd.Timestamp("2020-01-01")
WINDOWS = {
    "development": ("2020-02-04", "2026-06-01"),
    "validation": ("2026-06-01", "2026-11-01"),
}


def read_months(directory, prefix, names, months):
    frames = [
        pd.read_csv(directory / f"{prefix}-{month}.zip", header=None,
                    usecols=range(len(names)), names=names, dtype=str)
        for month in months
    ]
    frame = pd.concat(frames, ignore_index=True)
    # Binance futures monthly files may contain a CSV header.
    return frame.loc[frame[names[0]].str.fullmatch(r"\d+")].astype(
        {names[0]: "int64", **dict.fromkeys(names[1:], "float64")}
    )


def hold(candles):
    fee, slip = FEE_BPS / 10_000, SLIPPAGE_BPS / 10_000
    entry = candles.open.iloc[0] * (1 + slip)
    quantity = INITIAL / (entry * (1 + fee))
    exit_price = candles.close.iloc[-1] * (1 - slip)
    final_equity = quantity * exit_price * (1 - fee)
    equity = np.r_[INITIAL, candles.close.to_numpy() * quantity, final_equity]
    running_peak = np.maximum.accumulate(equity)
    pnl = final_equity - INITIAL
    return {
        "net_return_pct": 100 * pnl / INITIAL,
        "mdd_pct": 100 * np.max(1 - equity / running_peak),
        "profit_factor": 0.0 if pnl < 0 else None,
        "win_rate_pct": 100.0 if pnl > 0 else 0.0,
        "trade_count": 1,
        "max_consecutive_losses": int(pnl < 0),
        "fees_usdt": fee * quantity * (entry + exit_price),
        "funding_usdt": 0.0,
        "liquidation_count": 0,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="BTCUSDT-1m-, BTCUSDT-fundingRate-, mark-BTCUSDT-1h- monthly ZIPs and daily-mark-BTCUSDT-1h- ZIPs for gaps")
    args = parser.parse_args()

    data_end = pd.Timestamp("2026-11-01")
    months = pd.period_range(DATA_START, data_end - pd.Timedelta(days=1), freq="M")
    candles = read_months(args.directory, "BTCUSDT-1m", ["time", *CANDLE_COLUMNS], months)
    candles.index = pd.to_datetime(candles.pop("time"), unit="ms")
    validate_candles(candles, DATA_START, data_end)

    marks = read_months(args.directory, "mark-BTCUSDT-1h", ["time", "open"], months)
    daily_prefix = "daily-mark-BTCUSDT-1h"
    daily_dates = sorted(
        path.stem.removeprefix(daily_prefix + "-")
        for path in args.directory.glob(f"{daily_prefix}-*.zip")
    )
    if daily_dates:
        daily = read_months(args.directory, daily_prefix, ["time", "open"], daily_dates)
        marks = pd.concat([marks, daily.loc[~daily.time.isin(marks.time)]])
    marks.index = pd.to_datetime(marks.pop("time"), unit="ms")
    marks = marks.sort_index()
    expected_marks = pd.date_range(DATA_START, data_end, freq="h", inclusive="left")
    if not marks.index.equals(expected_marks) or not (np.isfinite(marks.open) & (marks.open > 0)).all():
        raise ValueError("시간봉 마크가격이 누락되었거나 유효하지 않습니다.")

    rates = read_months(args.directory, "BTCUSDT-fundingRate", ["time", "interval", "rate"], months)
    times = pd.to_datetime(rates.time, unit="ms").dt.floor("min")
    boundaries = pd.DatetimeIndex([DATA_START, *times, data_end])
    if (times != times.dt.floor("h")).any() or times.duplicated().any() or (
        not times.is_monotonic_increasing or
        (boundaries[1:] - boundaries[:-1] > pd.Timedelta(hours=8)).any()
    ) or not (np.isfinite(rates.rate) & (rates.rate.abs() <= 1)).all():
        raise ValueError("펀딩 이력에 공백이 있거나 유효하지 않습니다.")
    funding = dict(zip(times, zip(marks.open.reindex(times), rates.rate)))

    for label, (start, end) in WINDOWS.items():
        start, end = pd.Timestamp(start), pd.Timestamp(end)
        bars = candles.loc[(candles.index >= start) & (candles.index < end)]
        payments = {time: payment for time, payment in funding.items() if start <= time < end}
        rows = {"BTC_CASH_HOLD": hold(bars)}
        for name, strategy in STRATEGIES.items():
            inputs = candles.loc[start.floor("4h") - strategy.warmup:bars.index[-1]]
            signals = {time: signal for time, signal in strategy.signals(inputs).items()
                       if start <= time < end}
            for mode in ("both", "long") if strategy.loss_limits else ("both",):
                entries = signals if mode == "both" else {
                    time: signal for time, signal in signals.items() if signal.side == 1
                }
                result = replay(bars, entries, INITIAL, 1, FEE_BPS, SLIPPAGE_BPS, payments,
                                loss_limits=strategy.loss_limits, include_curve=False)
                trades = result["trades"]
                rows[f"{name}:{mode}"] = {
                    **result["metrics"],
                    "fees_usdt": sum(t["fees_usdt"] for t in trades),
                    "funding_usdt": sum(t["funding_cost_usdt"] for t in trades),
                }
        for row in rows.values():
            row["annualized_pct"] = 100 * (
                (1 + row["net_return_pct"] / 100) ** (365.25 / (end - start).days) - 1
            )
            row["fees_plus_funding_usdt"] = row["fees_usdt"] + row["funding_usdt"]
        print(json.dumps({
            "window": label, "start": start.isoformat(), "end_exclusive": end.isoformat(),
            "candle_count": len(bars), "funding_events": len(payments), "results": rows,
        }, allow_nan=False), flush=True)


if __name__ == "__main__":
    main()
