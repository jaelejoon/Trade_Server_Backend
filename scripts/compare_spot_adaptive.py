"""Offline walk-forward evaluation for the fixed V12 study."""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

import search_spot_2x as base
from search_spot_dd30 import targets_for

METHODS = {"WA": ("W-MON", False), "WR": ("W-MON", True),
           "MA": ("MS", False), "MR": ("MS", True)}
ANCHOR = pd.Timestamp("2018-09-01")
DAY = pd.Timedelta(days=1)
HOLD_DAYS = pd.Timedelta(days=28)


def replay(bars, schedule, fee_bps, shadow=False):
    """Mark daily for selection or every observed minute for evaluation."""
    fee = fee_bps / 10_000
    cash, quantity, peak, mdd = base.INITIAL, 0.0, base.INITIAL, 0.0
    count, fees, turnover, relative_turnover = 0, 0.0, 0.0, 0.0
    if shadow:
        marks = bars.close.resample("D").last().dropna()
        marks.index += DAY
        mark_times, mark_prices = marks.index, marks.to_numpy()
    else:
        mark_times, mark_prices = bars.index, bars.close.to_numpy()
    prices = bars.open.reindex(schedule.index)
    path = []
    cursor = 0

    def mark(end):
        nonlocal cursor, peak, mdd
        values = cash + quantity * mark_prices[cursor:end]
        if shadow:
            path.append(values)
        elif len(values):
            peaks = np.maximum.accumulate(np.maximum(values, peak))
            mdd = max(mdd, float((1 - values / peaks).max()))
            peak = peaks[-1]
        cursor = end

    for time, target in schedule.dropna().items():
        market = prices.at[time]
        if pd.isna(market) or time == bars.index[-1]:
            continue
        mark(mark_times.searchsorted(time, side="right" if shadow else "left"))
        buying = target * (cash + quantity * market) > quantity * market
        price = market * (1 + base.SLIP if buying else 1 - base.SLIP)
        nav_before = cash + quantity * market
        cash, after, traded = base.order_at_target(cash, quantity, price, target, fee)
        assert cash >= -1e-8 and 0 <= after
        notional = abs(after - quantity) * price
        count += traded
        fees += notional * fee
        turnover += notional
        relative_turnover += notional / nav_before
        quantity = after
    if shadow:
        mark(len(mark_times))
        return pd.Series(np.concatenate(path), index=mark_times)
    mark(len(mark_times) - 1)
    market = mark_prices[-1]
    last = market * (1 - base.SLIP)
    sold = base.RULES.can_sell(quantity, last)
    nav = cash + quantity * (last * (1 - fee) if sold else market)
    mdd = max(mdd, 1 - nav / max(peak, nav))
    notional = quantity * last if sold else 0
    return {"multiple": float(nav / base.INITIAL), "mdd_pct": float(mdd * 100),
            "orders": count, "terminal_sell": int(sold), "fee_usdt": float(fees + notional * fee),
            "turnover_usdt": float(turnover + notional),
            "relative_turnover": float(relative_turnover + notional / (cash + quantity * market))}


def recommendations(daily, reviews, rolling):
    result = {}
    for time in reviews:
        start = time - 365 * DAY if rolling else ANCHOR
        history = daily.loc[start:time]
        if start < ANCHOR or time - start < 365 * DAY or len(history) < 350 or time not in history.index:
            result[time] = "V12"
            continue
        returns = history.iloc[-1] / history.iloc[0] - 1
        mdd = (1 - history / history.cummax()).max()
        scores = returns - .5 * mdd
        best = max(("V35-45", "CASH"), key=scores.__getitem__)
        result[time] = best if scores[best] - scores["V12"] >= .05 else "V12"
    return pd.Series(result, dtype=object)


def build_schedule(targets, reviews, start, end):
    quarterly = targets.loc[(targets.index >= start) & (targets.index < end)
                            & ((targets.index == start) | (targets.index.month % 3 == 1))]
    reviews = reviews.loc[(reviews.index >= start) & (reviews.index < end)]
    mode, previous = "V12", None
    primary = defensive = 0.0
    orders = {}
    changes = 0
    for time in quarterly.index.union(reviews.index):
        if time in quarterly.index:
            row = quarterly.loc[time]
            if pd.notna(row["V12"]):
                primary = row["V12"]
            if pd.notna(row["V35-45"]):
                defensive = row["V35-45"]
        changed = False
        if time in reviews.index:
            suggested = reviews.at[time]
            if suggested != mode and (suggested == "V12" or previous is None or time - previous >= HOLD_DAYS):
                mode, previous = suggested, time
                changes += 1
                changed = True
        if time in quarterly.index or changed:
            target = primary
            if mode == "CASH":
                target = max(0.0, primary - .15)
            if mode == "V35-45":
                target = max(primary - .15, min(primary, (primary + defensive) / 2))
            orders[time] = target
    return pd.Series(orders, dtype=float), changes, len(reviews)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    candles, excluded = base.spot.load_spot(args.directory)
    targets = targets_for(candles)[["V12", "V35-45"]]
    shadow_bars = candles.loc[ANCHOR:]
    quarterly = targets.loc[(targets.index >= ANCHOR)
                             & ((targets.index == ANCHOR) | (targets.index.month % 3 == 1))]
    starts = pd.date_range(base.spot.SPOT_FIRST_WINDOW, base.END - pd.DateOffset(years=5), freq="MS")
    assert len(starts) == 51
    windows = [(start, start + pd.DateOffset(years=5)) for start in starts]
    windows.append((base.DEVELOPMENT_END, base.END))
    output = [{"start": str(start.date()), "end": str(end.date()), "fees": {}} for start, end in windows]
    for fee in base.FEES:
        daily = pd.DataFrame({name: replay(shadow_bars, quarterly[name], fee, shadow=True)
                              for name in targets})
        daily["CASH"] = base.INITIAL
        review = {name: recommendations(daily, pd.date_range(ANCHOR, base.END, freq=freq), rolling)
                  for name, (freq, rolling) in METHODS.items()}
        for (start, end), record in zip(windows, output, strict=True):
            bars = candles.iloc[candles.index.searchsorted(start):candles.index.searchsorted(end)]
            active = targets.loc[(targets.index >= start) & (targets.index < end)
                                 & ((targets.index == start) | (targets.index.month % 3 == 1))]
            rows = {n: replay(bars, active[n], fee) for n in active}
            rows["HOLD"] = replay(bars, pd.Series({bars.index[0]: 1.0}), fee)
            for name in METHODS:
                schedule, changes, reviews = build_schedule(targets, review[name], start, end)
                rows[name] = {**replay(bars, schedule, fee), "switches": changes, "reviews": reviews}
            record["fees"][str(fee)] = rows
    print(json.dumps({"candles": len(candles), "excluded": excluded, "windows": output},
                     allow_nan=False, indent=2))


if __name__ == "__main__":
    main()
