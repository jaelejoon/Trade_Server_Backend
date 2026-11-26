"""Compare the server against the pinned PR #15 scripts on the same spot ZIPs."""

import argparse
import csv
import math
import sys
from subprocess import check_output
from pathlib import Path

import pandas as pd

from app.services.spot_v12 import advance

PIN = "4bb47fdcecb3ce79f04bf835ca0564ba2c3945f3"


def compare(reference, directory, output):
    if check_output(["git", "-C", str(reference), "rev-parse", "HEAD"], text=True).strip() != PIN:
        raise ValueError("PR #15 참조 커밋이 일치하지 않습니다.")
    sys.path.insert(0, str(reference / "scripts"))
    import compare_spot_adaptive as oracle

    candles, excluded = oracle.base.spot.load_spot(directory)
    anchor, end = oracle.ANCHOR, oracle.base.END
    targets = oracle.targets_for(candles)[["V12", "V35-45"]]
    quarterly = targets.loc[(targets.index >= anchor)
                            & ((targets.index == anchor) | (targets.index.month % 3 == 1))]
    daily = pd.DataFrame({name: oracle.replay(candles.loc[anchor:], quarterly[name], 10.0, shadow=True)
                          for name in targets})
    daily["CASH"] = oracle.base.INITIAL
    reviews = oracle.recommendations(daily, pd.date_range(anchor, end, freq="W-MON"), False)
    closes = candles.close.resample("D").last().shift(1)
    del daily
    previous = None
    mode, last_change, switches = "V12", None, 0
    counts = {"match": 0, "mismatch": 0, "quarterly": 0, "weekly": 0, "both": 0, "switches": 0}
    writer = csv.writer(output)
    writer.writerow(["utc_date", "reference_mode", "server_mode", "reference_target",
                     "server_target", "reference_switch", "server_switch", "match"])
    for time in pd.date_range("2017-09-01", end - pd.Timedelta(days=1)):
        close = closes.get(time)
        opening = candles.open.get(time)
        previous, decision = advance(
            previous, time.date(), None if pd.isna(close) else float(close),
            time - pd.Timedelta(minutes=1) in candles.index,
            None if pd.isna(opening) else float(opening),
        )
        if decision is None:
            continue
        schedule, count, _ = oracle.build_schedule(targets, reviews, anchor, time + pd.Timedelta(days=1))
        if count != switches:
            mode, last_change = reviews.at[time], time
            switches = count
        if time.year < 2020:
            continue
        expected = float(schedule.iloc[-1])
        target = decision["target"]
        switched = last_change == time
        matched = (mode == decision["mode"] and switched == decision["changed"] and
                   target is not None and math.isclose(expected, target, abs_tol=1e-10, rel_tol=0))
        counts["match" if matched else "mismatch"] += 1
        counts["quarterly"] += decision["quarterly"]
        counts["weekly"] += decision["weekly"]
        counts["both"] += decision["weekly"] and decision["quarterly"]
        counts["switches"] += decision["changed"]
        writer.writerow([time.date(), mode, decision["mode"], expected, target, switched,
                         decision["changed"], matched])
    print({**counts, "candles": len(candles), "excluded": excluded, "reference": PIN}, file=sys.stderr)
    return counts["mismatch"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference", type=Path, help="Worktree at " + PIN)
    parser.add_argument("directory", type=Path, help="Binance BTCUSDT monthly 1m ZIP directory")
    args = parser.parse_args()
    raise SystemExit(bool(compare(args.reference.resolve(), args.directory, sys.stdout)))
