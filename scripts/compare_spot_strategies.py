"""Replay the existing BTC strategies on Binance spot BTCUSDT monthly 1m ZIPs."""

import argparse
from concurrent.futures import ProcessPoolExecutor
import json
from pathlib import Path

import numpy as np
import pandas as pd

from compare_strategies_1x import INITIAL, SLIPPAGE_BPS, read_months
from compare_strategies_1x_v2 import END, SPOT_FIRST_WINDOW, VALIDATION, eligible_bars
from app.services.backtest_engine import CANDLE_COLUMNS, STRATEGIES, replay
from app.services.spot_backtest import SpotOrderRules
from app.services.strategy_1x_regimes import replay_regime


FEE_SCENARIOS = {"standard": 10.0, "bnb": 7.5}
RULES = SpotOrderRules()
DATA = None
EVENTS = {}


def load_spot(directory):
    months = pd.period_range("2017-08", END - pd.Timedelta(days=1), freq="M")
    candles = read_months(directory, "BTCUSDT-1m", ["time", *CANDLE_COLUMNS], months)
    times = candles.pop("time")
    # Spot archive timestamps switched to microseconds in 2025.
    times = times.where(times < 10**14, times // 1000)
    candles.index = pd.to_datetime(times, unit="ms")
    prices = candles[["open", "high", "low", "close"]]
    if (not candles.index.is_unique or not candles.index.is_monotonic_increasing
            or candles.index[-1] >= END or not np.isfinite(candles.to_numpy()).all()
            or (prices <= 0).any().any() or (candles.volume < 0).any()
            or (candles.high < prices.max(axis=1)).any()
            or (candles.low > prices.min(axis=1)).any()):
        raise ValueError("현물 분봉 데이터가 유효하지 않습니다.")
    # Old archives contain shifted 60-second bars. Their opens are not minute-boundary fills.
    aligned = candles.index == candles.index.floor("min")
    return candles.loc[aligned], int((~aligned).sum())


def evaluate(window):
    label, start, end = window
    bars = DATA.iloc[DATA.index.searchsorted(start):DATA.index.searchsorted(end)]
    years = (end - start).days / 365.25
    outputs = {}
    for scenario, fee in FEE_SCENARIOS.items():
        rows = {"BTC_CASH_HOLD": replay_regime(
            bars, {bars.index[0]: True}, 1, INITIAL, fee, SLIPPAGE_BPS, {}, curve=False, spot=RULES,
        )}
        for name, (strategy, events) in EVENTS.items():
            signals = {t: event for t, event in events.items() if start <= t < end}
            if strategy.regime:
                result = replay_regime(
                    bars, signals, strategy.regime.exposure, INITIAL, fee, SLIPPAGE_BPS, {},
                    curve=False, spot=RULES,
                )
            else:
                signals = {t: signal for t, signal in signals.items() if t in bars.index}
                sparse = eligible_bars(bars, signals)
                result = replay(sparse, signals, INITIAL, 1, fee, SLIPPAGE_BPS, {},
                                loss_limits=strategy.loss_limits, include_curve=False, spot=RULES)
                # A rejected exit can keep a position open beyond its expected expiry.
                if result["metrics"]["rejected_sells"]:
                    result = replay(bars, signals, INITIAL, 1, fee, SLIPPAGE_BPS, {},
                                    loss_limits=strategy.loss_limits, include_curve=False, spot=RULES)
            rows[name] = result
        outputs[scenario] = {}
        for name, result in rows.items():
            metrics = result["metrics"]
            outputs[scenario][name] = {
                **metrics,
                "multiple": metrics["final_equity_usdt"] / INITIAL,
                "annual_trades": metrics["trade_count"] / years,
                "fees_usdt": (sum(t["fees_usdt"] for t in result["trades"])
                              + metrics["open_entry_fee_usdt"]),
            }
    return {
        "window": label, "start": str(start.date()), "end_exclusive": str(end.date()),
        "candles": len(bars), "missing_minutes": int((end - start) / pd.Timedelta(minutes=1)) - len(bars),
        "results": outputs,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="Directory of spot BTCUSDT-1m-YYYY-MM.zip archives")
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()
    global DATA
    DATA, discarded = load_spot(args.directory)
    minutes = int((END - DATA.index[0]) / pd.Timedelta(minutes=1))
    print(json.dumps({"first": str(DATA.index[0]), "last": str(DATA.index[-1]),
                      "candles": len(DATA), "missing_minutes": minutes - len(DATA),
                      "discarded_off_minute": discarded}), flush=True)
    for name, strategy in STRATEGIES.items():
        if strategy.regime:
            EVENTS[name] = (strategy, strategy.regime.decisions(DATA))
        elif strategy.loss_limits:
            EVENTS[name] = (strategy, {t: s for t, s in strategy.signals(DATA).items() if s.side == 1})
    tasks = [("validation", VALIDATION, END)]
    tasks.extend(("rolling", start, start + pd.DateOffset(years=5)) for start in
                 pd.date_range(SPOT_FIRST_WINDOW, END - pd.DateOffset(years=5), freq="MS"))
    if args.workers == 1:
        for task in tasks:
            print(json.dumps(evaluate(task), allow_nan=False), flush=True)
    else:
        from multiprocessing import get_context
        with ProcessPoolExecutor(max_workers=args.workers, mp_context=get_context("fork")) as pool:
            for row in pool.map(evaluate, tasks):
                print(json.dumps(row, allow_nan=False), flush=True)


if __name__ == "__main__":
    main()
