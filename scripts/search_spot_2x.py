"""Run the fixed BTC spot study in docs/spot-2x-plan.md."""
import argparse
import hashlib
import json
from pathlib import Path
from statistics import median

import numpy as np
import pandas as pd

import compare_spot_strategies as spot
from app.services.strategy_1x_regimes import REGIMES, replay_regime


DEVELOPMENT_END = pd.Timestamp("2024-01-01")
END = spot.END
INITIAL = spot.INITIAL
FEES = (10.0, 7.5)
CANDIDATES = ("T6", "T12", "M6", "M12", "V6", "V12", "R75", "R90", "D40", "D60")
QUARTERLY = {"V6", "V12", "R75", "R90", "D40", "D60"}
REFERENCES = ("HOLD", "V2")
RULES = spot.RULES
SLIP = spot.SLIPPAGE_BPS / 10_000


def monthly_targets(candles):
    closes = candles.close.resample("MS").last()
    dates = closes.index + pd.offsets.MonthBegin()
    closes = closes.where((dates - pd.Timedelta(minutes=1)).isin(candles.index))
    mean6, mean12 = (closes.rolling(n).mean() for n in (6, 12))
    mom3, mom6 = (closes / closes.shift(n) - 1 for n in (3, 6))
    volatility = closes.pct_change(fill_method=None).rolling(6).std() * np.sqrt(12)
    drawdown = 1 - closes / closes.rolling(6).max()
    targets = pd.DataFrame(index=closes.index)
    for n, trend, momentum, high, goal in (
        (6, mean6, mom3, .85, .55), (12, mean12, mom6, 1, .70)
    ):
        up = closes > trend
        targets[f"T{n}"] = pd.Series(np.where(up, high, .20), index=closes.index).where(trend.notna())
        targets[f"M{n}"] = (up & momentum.gt(0)).astype(float).where(trend.notna() & momentum.notna())
        ratio = goal / volatility
        targets[f"V{n}"] = ratio.clip(upper=1).where(up, 0).where(trend.notna() & volatility.notna())
    targets["R75"], targets["R90"] = .75, .90
    for key, low, middle, first, second in (
        ("D40", .40, .70, .20, .35), ("D60", .60, .80, .15, .30)
    ):
        weights = np.select([drawdown.lt(first), drawdown.lt(second)], [low, middle], default=1.0)
        targets[key] = pd.Series(weights, index=closes.index).where(drawdown.notna())
    targets.index = dates
    return targets


def order_at_target(cash, quantity, price, target, fee):
    nav = cash + quantity * price
    difference = target * nav - quantity * price
    if difference > 0:
        requested = min(cash / (price * (1 + fee)), difference / (price * (1 + target * fee)))
        filled = RULES.buy_quantity(requested, price)
        return cash - filled * price * (1 + fee), round(quantity + filled, 5), bool(filled)
    requested = min(quantity, -difference / (price * (1 - target * fee)))
    filled = RULES.buy_quantity(requested, price)
    return cash + filled * price * (1 - fee), round(quantity - filled, 5), bool(filled)


def replay_targets(bars, schedule, quarterly, fee_bps):
    cash, quantity, peak, mdd, orders = INITIAL, 0.0, INITIAL, 0.0, 0
    fee = fee_bps / 10_000
    opens, closes = bars.open.to_numpy(), bars.close.to_numpy()
    cursor = 0

    def mark(values):
        nonlocal peak, mdd
        if len(values):
            peaks = np.maximum.accumulate(np.maximum(values, peak))
            mdd = max(mdd, float((1 - values / peaks).max()))
            peak = peaks[-1]

    last_target = None
    for time, target in schedule.dropna().items():
        i = bars.index.get_indexer([time])[0]
        if i < 0 or i == len(bars) - 1:
            continue
        if not quarterly and target == last_target:
            continue
        mark(cash + quantity * closes[cursor:i] if quantity else [cash])
        price = opens[i] * (1 + SLIP if target * (cash + quantity * opens[i]) > quantity * opens[i] else 1 - SLIP)
        cash, quantity, traded = order_at_target(cash, quantity, price, target, fee)
        assert cash >= -1e-8 and quantity >= -1e-10
        orders += traded
        last_target, cursor = target, i

    mark(cash + quantity * closes[cursor:-1] if quantity else [cash])
    last_price = closes[-1] * (1 - SLIP)
    exit_filled = RULES.can_sell(quantity, last_price)
    nav = cash + quantity * (last_price * (1 - fee) if exit_filled else closes[-1])
    mark([nav])
    return nav / INITIAL, mdd * 100, orders, int(exit_filled)


def evaluate_window(candles, targets, v2, start, end, fees):
    bars = candles.iloc[candles.index.searchsorted(start):candles.index.searchsorted(end)]
    monthly = targets.loc[(targets.index >= start) & (targets.index < end)]
    quarterly = monthly.loc[(monthly.index == start) | ((monthly.index.month - 1) % 3 == 0)]
    results = {}
    years = ((end.year - start.year) * 12 + end.month - start.month) / 12
    for fee in fees:
        rows = {}
        for name in CANDIDATES:
            is_quarterly = name in QUARTERLY
            rows[name] = replay_targets(bars, (quarterly if is_quarterly else monthly)[name], is_quarterly, fee)
        for name, exposure, decisions in (
            ("HOLD", 1, {bars.index[0]: True}),
            ("V2", .55, {t: v for t, v in v2.items() if start <= t < end}),
        ):
            result = replay_regime(bars, decisions, exposure, INITIAL, fee, spot.SLIPPAGE_BPS, {},
                                   curve=False, spot=RULES)
            metrics = result["metrics"]
            end_exit = sum(t["exit_reason"] == "end_of_period" for t in result["trades"])
            rows[name] = (metrics["final_equity_usdt"] / INITIAL, metrics["mdd_pct"],
                          metrics["trade_count"] * 2 - end_exit, end_exit)
        results[str(fee)] = {
            n: {"multiple": m, "mdd_pct": d, "orders": o, "terminal_sell": t, "annual_orders": o / years}
            for n, (m, d, o, t) in rows.items()
        }
    return {"start": str(start.date()), "end_exclusive": str(end.date()), "metrics": results}


def run(directory, phase, selection_path):
    digest = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if phase == "development":
        spot.END = DEVELOPMENT_END
        fees = FEES[:1]
        end = DEVELOPMENT_END
    else:
        selection = json.loads(selection_path.read_text())
        if selection["script_sha256"] != digest:
            raise ValueError("선정 이후 분석 코드가 달라졌습니다.")
        fees, end = FEES, END
    candles, discarded = spot.load_spot(directory)
    targets = monthly_targets(candles)
    v2 = REGIMES["SAFE_1X_DEFENSIVE_V2"].decisions(candles)
    starts = pd.date_range(spot.SPOT_FIRST_WINDOW, end - pd.DateOffset(years=5), freq="MS")
    rolling = [evaluate_window(candles, targets, v2, start, start + pd.DateOffset(years=5), fees)
               for start in starts]
    if phase == "development":
        assert len(rolling) == 17
        ranking = []
        for name in CANDIDATES:
            rows = [r["metrics"][str(fees[0])][name] for r in rolling]
            multiples = [r["multiple"] for r in rows]
            ranking.append({"candidate": name, "hits": sum(bool(m >= 2) for m in multiples),
                            "min": min(multiples), "median": median(multiples),
                            "eligible": all(r["annual_orders"] <= 4 for r in rows)})
        ranking.sort(key=lambda r: (r["eligible"], r["hits"], r["min"], r["median"]), reverse=True)
        output = {"script_sha256": digest, "selected": ranking[0]["candidate"] if ranking[0]["eligible"] else None,
                  "ranking": ranking, "rolling": rolling}
        serialized = json.dumps(output, allow_nan=False, indent=2)
        with selection_path.open("x") as file:
            file.write(serialized)
        return {k: v for k, v in output.items() if k != "rolling"}
    assert len(rolling) == 51
    for before, after in zip(selection["rolling"], rolling, strict=False):
        if before["metrics"]["10.0"] != after["metrics"]["10.0"]:
            raise ValueError("전체 데이터 재생에서 개발 창 결과가 달라졌습니다.")
    return {"selected": selection["selected"], "candles": len(candles), "off_minute": discarded,
            "rolling": rolling, "validation": evaluate_window(candles, targets, v2, DEVELOPMENT_END, END, fees)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("phase", choices=("development", "evaluation"))
    parser.add_argument("selection", type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.directory, args.phase, args.selection), allow_nan=False, indent=2))


if __name__ == "__main__":
    main()
