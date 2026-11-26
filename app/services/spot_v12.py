"""V12 and weekly anchored walk-forward decisions from completed UTC days."""

from copy import deepcopy
from datetime import date, timedelta
from math import sqrt
from statistics import mean, stdev

from app.services.spot_backtest import SpotOrderRules

RULES = SpotOrderRules()
ANCHOR = date(2018, 9, 1)
DAY = timedelta(days=1)
VERSION = "spot-v12-wa-1"


def quarter(day):
    return day.day == 1 and day.month in (1, 4, 7, 10)


def monthly_targets(closes):
    if len(closes) < 12 or any(value is None for value in closes[-12:]):
        return None, None, None, None
    average = mean(closes[-12:])
    returns = [right / left - 1 for left, right in zip(closes[-7:-1], closes[-6:])]
    vol = stdev(returns) * sqrt(12)
    if closes[-1] <= average:
        return 0., 0., average, vol
    return min(1., .70 / vol) if vol else 1., min(.45, .35 / vol) if vol else .45, average, vol


def _shadow_trade(book, market, target, fee):
    cash, quantity = book["cash"], book["quantity"]
    buy = target * (cash + quantity * market) > quantity * market
    price = market * (1.0002 if buy else .9998)
    difference = target * (cash + quantity * price) - quantity * price
    if difference > 0:
        amount = min(cash / (price * (1 + fee)), difference / (price * (1 + target * fee)))
    else:
        amount = min(quantity, -difference / (price * (1 - target * fee)))
    amount = RULES.buy_quantity(amount, price)
    if not amount:
        return
    if difference > 0:
        book.update(cash=cash - amount * price * (1 + fee), quantity=round(quantity + amount, 5))
    else:
        book.update(cash=cash + amount * price * (1 - fee), quantity=round(quantity - amount, 5))


def advance(previous, day, prior_close, exact_close, boundary_open, fee=.001):
    """Process one UTC boundary. Shadow equity is marked before the boundary trade."""
    state = deepcopy(previous) if previous else {
        "closes": [], "primary": None, "defensive": None, "mode": "V12",
        "changed_at": None, "observations": 0, "average": None, "volatility": None,
        "books": {name: {"cash": 10000., "quantity": 0., "first": None, "peak": 0., "mdd": 0., "nav": None}
                  for name in ("V12", "V35-45", "CASH")},
    }
    if previous and date.fromisoformat(previous["boundary"]) + DAY != day:
        raise ValueError("UTC 날짜를 순서대로 처리해야 합니다.")
    state["boundary"] = day.isoformat()
    if day.day == 1:
        state["closes"] = (state["closes"] + [prior_close if exact_close else None])[-12:]
    quarterly, weekly = quarter(day), day.weekday() == 0
    update_base = quarterly or day == ANCHOR
    primary, defensive, average, vol = monthly_targets(state["closes"])
    if update_base:
        state["average"], state["volatility"] = average, vol
        if primary is not None:
            state["primary"], state["defensive"] = primary, defensive

    if day > ANCHOR and prior_close is not None:
        state["observations"] += 1
        for book in state["books"].values():
            nav = book["cash"] + book["quantity"] * prior_close
            if book["first"] is None:
                book["first"] = nav
            book["nav"] = nav
            book["peak"] = max(book["peak"], nav)
            book["mdd"] = max(book["mdd"], 1 - nav / book["peak"])

    scores = {name: None if book["first"] is None else book["nav"] / book["first"] - 1 - .5 * book["mdd"]
              for name, book in state["books"].items()}
    changed = False
    suggested = "V12"
    if weekly and day >= ANCHOR:
        if day - ANCHOR >= timedelta(days=365) and state["observations"] >= 350 and prior_close is not None:
            candidate = max(("V35-45", "CASH"), key=scores.__getitem__)
            if scores[candidate] - scores["V12"] >= .05:
                suggested = candidate
        last = state["changed_at"]
        changed = suggested != state["mode"] and (
            suggested == "V12" or last is None or day - date.fromisoformat(last) >= timedelta(days=28)
        )
        if changed:
            state.update(mode=suggested, changed_at=day.isoformat())

    if update_base and primary is not None and boundary_open is not None and day >= ANCHOR:
        for name, target in (("V12", primary), ("V35-45", defensive)):
            _shadow_trade(state["books"][name], boundary_open, target, fee)

    target = state["primary"]
    if target is not None:
        if state["mode"] == "CASH":
            target = max(0., target - .15)
        elif state["mode"] == "V35-45":
            target = max(target - .15, min(target, (target + state["defensive"]) / 2))
    result = None
    if day >= ANCHOR and (quarterly or weekly):
        result = {
            "decision_id": f"{VERSION}:{day.isoformat()}",
            "version": VERSION, "mode": state["mode"], "suggested": suggested if weekly else None,
            "changed_at": state["changed_at"], "target": target, "base_target": state["primary"],
            "average_12m": state["average"], "volatility": state["volatility"], "scores": scores,
            "observations": state["observations"], "boundary": day.isoformat(),
            "quarterly": quarterly, "weekly": weekly, "changed": changed,
            "request": (quarterly or changed) and target is not None,
            "data_valid": prior_close is not None and (not quarterly or primary is not None),
        }
    return state, result


def rebalance(cash, owned, free_btc, price, target, fee, cap):
    """A fee-aware intent capped by journal-owned, available BTC and the testnet cap."""
    exposure = owned * price
    difference = target * (cash + exposure) - exposure
    if difference > 0:
        return "BUY", min(cash / (price * (1 + fee)),
                          difference / (price * (1 + target * fee)), cap / price)
    return "SELL", min(owned, free_btc, -difference / (price * (1 - target * fee)), cap / price)
