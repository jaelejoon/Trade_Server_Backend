"""python scripts/backtest_csv.py btc_1m.csv [--funding-json funding.json]"""
import argparse
import contextlib
import io
import json
import math
from pathlib import Path
import sys

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.strategy.strategy_v57 import backtest, build_signal_events, calculate_stats, prepare_data


def main():
    parser = argparse.ArgumentParser(description='수정된 V57 엔진으로 1분봉 CSV를 검사합니다.')
    parser.add_argument('csv', type=Path)
    parser.add_argument('--funding-json', type=Path, help='Binance fundingTime/fundingRate/markPrice 배열')
    args = parser.parse_args()
    raw = pd.read_csv(args.csv)
    funding = json.loads(args.funding_json.read_text()) if args.funding_json else None
    diagnostics = io.StringIO()
    with contextlib.redirect_stdout(diagnostics):
        hours, bars = prepare_data(raw)
        events = build_signal_events(hours, bars)
        equity, trades = backtest(bars, events, funding_rates=funding)
        stats = calculate_stats(trades, equity)
    stats = {key: float(value) if isinstance(value, (int, float)) else value for key, value in stats.items()}
    for key, value in stats.items():
        if isinstance(value, float) and not math.isfinite(value):
            stats[key] = None
    print(diagnostics.getvalue(), end='')
    print(json.dumps({
        'engine_revision': '2026-11-24-causal',
        'funding_included': funding is not None,
        'signal_count': len(events),
        'position_limit': 1,
        'stats': stats,
        'limitations': ['15분 OHLC 근사 체결', '실제 청산 엔진과 체결 지연 미재현'] + ([] if funding is not None else ['펀딩비 미포함']),
    }, ensure_ascii=False, indent=2, default=str, allow_nan=False))


if __name__ == '__main__':
    main()
