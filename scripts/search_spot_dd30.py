"""Evaluate the fixed candidates in docs/spot-dd30-plan.md."""
import argparse
import hashlib
import json
from pathlib import Path
from statistics import median

import numpy as np
import pandas as pd

import search_spot_2x as base
from app.services.strategy_1x_regimes import REGIMES


VOLATILITY = tuple((f"V{goal}-{cap}", goal / 100, cap / 100)
                   for goal in (35, 50) for cap in (45, 60))
TREND = tuple((f"T{high}-{low}", high / 100, low / 100)
              for high in (40, 55, 70) for low in (0, 10))
DUAL = tuple((f"D{weight}{period}", weight / 100, period)
             for weight in (45, 60, 75) for period in ("M", "Q"))
NEW = tuple(row[0] for row in (*VOLATILITY, *TREND, *DUAL))
QUARTERLY = {name for name, _, _ in VOLATILITY} | {
    name for name, _, period in DUAL if period == "Q"
} | {"V12"}


def targets_for(candles):
    targets = base.monthly_targets(candles)[["V12", "T12"]].copy()
    closes = candles.close.resample("MS").last()
    closes.index = targets.index
    closes = closes.where((closes.index - pd.Timedelta(minutes=1)).isin(candles.index))
    mean6, mean12 = (closes.rolling(n).mean() for n in (6, 12))
    up12 = closes.gt(mean12)
    dual_up = up12 & closes.gt(mean6)
    vol = closes.pct_change(fill_method=None).rolling(6).std() * np.sqrt(12)
    for name, goal, cap in VOLATILITY:
        targets[name] = (goal / vol).clip(upper=cap).where(up12, 0).where(mean12.notna() & vol.notna())
    for name, high, low in TREND:
        targets[name] = pd.Series(np.where(up12, high, low), index=closes.index).where(mean12.notna())
    for name, weight, _ in DUAL:
        targets[name] = (dual_up.astype(float) * weight).where(mean12.notna() & mean6.notna())
    return targets


def run(directory, phase, selection_path):
    digest = hashlib.sha256(Path(__file__).read_bytes() + Path(base.__file__).read_bytes()).hexdigest()
    development = phase == "development"
    if not development:
        selection = json.loads(selection_path.read_text())
        if selection["sha256"] != digest:
            raise ValueError("선정 때의 코드와 다릅니다.")
    end = base.DEVELOPMENT_END if development else base.END
    fees = base.FEES[:1] if development else base.FEES
    original = (base.spot.END, base.CANDIDATES, base.QUARTERLY)
    try:
        base.spot.END = end
        base.CANDIDATES, base.QUARTERLY = (*NEW, "V12", "T12"), QUARTERLY
        candles, excluded = base.spot.load_spot(directory)
        targets = targets_for(candles)
        decisions = REGIMES["SAFE_1X_DEFENSIVE_V2"].decisions(candles)
        starts = pd.date_range(base.spot.SPOT_FIRST_WINDOW, end - pd.DateOffset(years=5), freq="MS")
        assert len(starts) == (17 if development else 51)
        rolling = [base.evaluate_window(candles, targets, decisions, start,
                                        start + pd.DateOffset(years=5), fees) for start in starts]
        if development:
            ranking = []
            for name in NEW:
                rows = [window["metrics"]["10.0"][name] for window in rolling]
                multiples = [float(row["multiple"]) for row in rows]
                max_mdd = max(row["mdd_pct"] for row in rows)
                frequency = max(row["annual_orders"] for row in rows)
                ranking.append({"name": name, "hits": sum(value >= 2 for value in multiples),
                                "min": min(multiples), "median": median(multiples), "max_mdd": max_mdd,
                                "max_annual_orders": frequency, "eligible": max_mdd <= 30 and frequency <= 4})
            ranking.sort(key=lambda row: (row["eligible"], row["hits"], row["min"], row["median"]), reverse=True)
            chosen = ranking[0]["name"] if ranking[0]["eligible"] else None
            result = {"sha256": digest, "selected": chosen, "ranking": ranking}
            detailed = json.dumps({**result, "rolling": rolling}, indent=2, allow_nan=False)
            with selection_path.open("x") as output:
                output.write(detailed)
            return result
        assert len(selection["rolling"]) == 17
        for old, new in zip(selection["rolling"], rolling):
            assert old["start"] == new["start"] and old["metrics"]["10.0"] == new["metrics"]["10.0"]
        return {"selected": selection["selected"], "candles": len(candles), "excluded": excluded,
                "rolling": rolling,
                "validation": base.evaluate_window(candles, targets, decisions, base.DEVELOPMENT_END, end, fees)}
    finally:
        base.spot.END, base.CANDIDATES, base.QUARTERLY = original


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("phase", choices=("development", "evaluation"))
    parser.add_argument("selection", type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.directory, args.phase, args.selection), indent=2, allow_nan=False))
