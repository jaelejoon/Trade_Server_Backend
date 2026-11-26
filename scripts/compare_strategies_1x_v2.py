"""Five-year monthly rolling BTCUSDT replay, using local Binance archives only."""

import argparse
from concurrent.futures import ProcessPoolExecutor
import json
from pathlib import Path

import numpy as np
import pandas as pd

from compare_strategies_1x import (
    DATA_START, FEE_BPS, INITIAL, SLIPPAGE_BPS, hold, load_futures, read_months,
)
from app.services.backtest_engine import CANDLE_COLUMNS, STRATEGIES, replay
from app.services.strategy_1x_regimes import replay_regime


END = pd.Timestamp("2026-11-01")
VALIDATION = pd.Timestamp("2024-01-01")
SPOT_FIRST_WINDOW = pd.Timestamp("2017-09-01")
DATASETS = {}
FUNDING = {}


def eligible_bars(bars, signals):
    """Flat spans with no eligible entry cannot affect legacy replay metrics."""
    if not signals:
        return bars.iloc[[0, -1]]
    index = bars.index
    starts = pd.DatetimeIndex(list(signals))
    expires = starts + pd.to_timedelta([s.max_hold_minutes for s in signals.values()], unit="min")
    left = index.get_indexer(starts)
    right = np.minimum(index.searchsorted(expires) + 1, len(index))
    changes = np.zeros(len(index) + 1, dtype=np.int32)
    np.add.at(changes, left, 1)
    np.add.at(changes, right, -1)
    keep = changes[:-1].cumsum() > 0
    keep[[0, -1]] = True
    return bars.loc[keep]


def evaluate(task):
    group, start, end = task
    data, strategies = DATASETS[group]
    bars = data.iloc[data.index.searchsorted(start):data.index.searchsorted(end)]
    funding = {t: p for t, p in FUNDING.items() if start <= t < end}
    years = (end - start).days / 365.25
    benchmark = hold(bars)
    results = {"BTC_CASH_HOLD": {
        **benchmark, "annual_cost_pct": (benchmark["fees_usdt"] / INITIAL * 100) / years,
    }}
    for name, (strategy, events) in strategies.items():
        signals = {t: signal for t, signal in events.items() if start <= t < end}
        if strategy.regime:
            result = replay_regime(
                bars, signals, strategy.regime.exposure, INITIAL, FEE_BPS,
                SLIPPAGE_BPS, funding, curve=False,
            )
        else:
            signals = {t: s for t, s in signals.items()
                       if t in bars.index and (t >= DATA_START or s.side == 1)}
            result = replay(
                eligible_bars(bars, signals), signals, INITIAL, 1, FEE_BPS,
                SLIPPAGE_BPS, funding, loss_limits=strategy.loss_limits, include_curve=False,
            )
        fees = sum(t["fees_usdt"] for t in result["trades"])
        net_funding = sum(t["funding_cost_usdt"] for t in result["trades"])
        results[name] = {
            **result["metrics"], "fees_usdt": fees, "funding_usdt": net_funding,
            "annual_cost_pct": (fees + net_funding) / INITIAL * 100 / years,
        }
    for row in results.values():
        row["multiple"] = 1 + row["net_return_pct"] / 100
        row["annual_trades"] = row["trade_count"] / years
    return {
        "group": group, "start": str(start.date()), "end_exclusive": str(end.date()),
        "development_only": end <= VALIDATION, "candle_count": len(bars),
        "funding_count": len(funding), "results": results,
    }


def prepare(group, data):
    strategies = {}
    for name, strategy in STRATEGIES.items():
        events = strategy.regime.decisions(data) if strategy.regime else strategy.signals(data)
        strategies[name] = (strategy, events)
    DATASETS[group] = (data, strategies)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="Futures ZIPs per compare_strategies_1x.py; spot monthly ZIPs in spot/")
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()
    futures, funding = load_futures(args.directory)
    FUNDING.update(funding)
    months = pd.period_range("2017-08", "2019-12", freq="M")
    spot = read_months(args.directory / "spot", "BTCUSDT-1m", ["time", *CANDLE_COLUMNS], months)
    spot.index = pd.to_datetime(spot.pop("time"), unit="ms")
    if not spot.index.is_unique or not spot.index.is_monotonic_increasing or spot.index.max() >= DATA_START:
        raise ValueError("현물 분봉 시각이 유효하지 않습니다.")
    if not np.isfinite(spot.to_numpy()).all() or (spot[["open", "high", "low", "close"]] <= 0).any().any():
        raise ValueError("현물 가격이 유효하지 않습니다.")
    hybrid = pd.concat([spot, futures])
    expected_spot = int((DATA_START - spot.index[0]) / pd.Timedelta(minutes=1))
    print(json.dumps({
        "input": {"spot_first": str(spot.index[0]), "spot_candles": len(spot),
                  "spot_missing_minutes": expected_spot - len(spot), "futures_candles": len(futures),
                  "funding_records": len(funding)},
    }), flush=True)
    prepare("spot_proxy", hybrid)
    prepare("futures", futures)
    DATASETS["validation"] = DATASETS["futures"]

    starts = pd.date_range(SPOT_FIRST_WINDOW, END - pd.DateOffset(years=5), freq="MS")
    tasks = [("validation", VALIDATION, END)]
    tasks.extend(("spot_proxy" if start < DATA_START else "futures",
                  start, start + pd.DateOffset(years=5)) for start in starts)
    if args.workers == 1:
        for task in tasks:
            print(json.dumps(evaluate(task), allow_nan=False), flush=True)
    else:
        # Sharing precomputed candle arrays across workers avoids copying the archive.
        from multiprocessing import get_context
        with ProcessPoolExecutor(max_workers=args.workers, mp_context=get_context("fork")) as pool:
            for row in pool.map(evaluate, tasks):
                print(json.dumps(row, allow_nan=False), flush=True)


if __name__ == "__main__":
    main()
