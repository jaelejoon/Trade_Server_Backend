from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.services.spot_v12 import advance, monthly_targets, rebalance
from app.services import spot_v12_runtime as runtime


def test_monthly_trend_volatility_and_closed_history():
    assert monthly_targets(list(range(12, 0, -1)))[:2] == (0, 0)
    base, defensive, average, volatility = monthly_targets(list(range(1, 13)))
    assert (base, defensive, average) == (1, .45, 6.5)
    assert volatility > 0
    assert monthly_targets([None] + list(range(1, 12)))[:2] == (None, None)


def test_future_prices_do_not_change_past_decision():
    def replay(future):
        state, records = None, {}
        start = date(2017, 9, 1)
        for i in range(1200):
            day = start + timedelta(days=i)
            price = 100 + i if i < 900 else future
            state, decision = advance(state, day, price, True, price)
            if decision:
                records[day] = decision
        return records

    first, second = replay(2), replay(10000)
    earlier = [day for day in first if day < date(2017, 9, 1) + timedelta(days=900)]
    assert len(earlier) == 80
    assert all(first[day] == second[day] for day in earlier)


def test_weekly_hold_and_immediate_base_return():
    state, _ = advance(None, date(2020, 1, 5), 100, True, 100)
    state.update(primary=.8, defensive=.35, observations=400, mode="V35-45", changed_at="2020-01-05")
    state["books"]["V12"].update(cash=9000, first=10000, peak=10000)
    state["books"]["V35-45"].update(cash=9000, first=10000, peak=10000)
    state["books"]["CASH"].update(first=10000, peak=10000)
    _, review = advance(state, date(2020, 1, 6), 100, True, 100)
    assert (review["suggested"], review["mode"], review["changed"]) == ("CASH", "V35-45", False)
    assert review["target"] == pytest.approx(.65)
    state["books"]["V12"]["cash"] = 11000
    _, review = advance(state, date(2020, 1, 6), 100, True, 100)
    assert (review["mode"], review["changed"], review["target"]) == ("V12", True, .8)


def test_rebalance_sell_cap_and_paper_fee():
    side, qty = rebalance(Decimal(0), Decimal(".02"), Decimal(".001"), Decimal(1000),
                          Decimal(0), Decimal(".001"), Decimal(12))
    assert (side, qty) == ("SELL", Decimal(".001"))
    book = {"cash": "100", "quantity": "0"}
    buy = runtime.paper_intent(book, {"target": 1}, 1000)
    assert buy["status"] == "simulated" and Decimal(buy["notional"]) <= 12
    assert Decimal(book["cash"]) == 100 - Decimal(buy["notional"]) - Decimal(buy["fee"])
    sale = runtime.paper_intent(book, {"target": 0}, 1000)
    assert sale["side"] == "SELL" and Decimal(book["quantity"]) == 0


def test_pending_testnet_order_restart_only_looks_up(monkeypatch):
    db, exchange = Mock(), Mock()
    account = SimpleNamespace(is_running=True)
    decision = {"boundary": "2026-01-05", "execution": {"status": "pending", "side": "BUY", "client_id": "id", "decision_id": "d", "notional": "10"}}
    state = SimpleNamespace(runtime_state={"v12_wa": {"decision": decision}})
    exchange.lookup.return_value = None
    outcome = runtime._finish_testnet(db, account, state, exchange, submit=False)
    assert outcome["status"] == "unconfirmed" and account.is_running is False
    exchange.lookup.assert_called_once_with("id")
    exchange.market_buy_quote.assert_not_called()
    exchange.order.assert_not_called()
    db.commit.assert_called_once()


def test_same_boundary_records_one_paper_intent(monkeypatch):
    from datetime import datetime, timezone

    boundary = datetime(2026, 1, 1, tzinfo=timezone.utc)
    now = boundary + timedelta(minutes=2)
    model, _ = advance(None, date(2025, 12, 31), 100, True, 100)
    model["closes"] = list(range(100, 111))
    account = SimpleNamespace(id=2, user_id=3, is_running=True, is_active=True, market_type="SPOT")
    state = SimpleNamespace(runtime_state={"v12_wa": {"model": model}, "v12_controls": {"enabled": True}})
    db = Mock()
    db.scalar.return_value = state
    db.execute.return_value.mappings.return_value = [
        {"day": boundary.date(), "close": 120, "exact_close": True, "open": 120}
    ]
    monkeypatch.setattr(runtime, "DRY_RUN", True)
    monkeypatch.setattr(runtime, "ENABLE_SPOT_TESTNET_ORDERS", False)
    assert runtime._calculate(db, account, now, boundary, boundary.date(), boundary.date(), False)[
        "decision"]["execution"]["status"] == "simulated"
    db.scalar.side_effect = [state, 120]
    assert runtime._calculate(db, account, now, boundary, boundary.date(), boundary.date(), False)[
        "status"] == "up_to_date"
    db.commit.assert_called_once()
    assert len(state.runtime_state["v12_wa"]["history"]) == 1


def test_postgresql_daily_cutoff_ignores_open_boundary_minute():
    import os
    from datetime import datetime
    from sqlalchemy import create_engine, text

    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated PostgreSQL required")
    engine = create_engine(url)
    try:
        with engine.begin() as db:
            db.execute(text("""CREATE TEMP TABLE spot_candles_1m
                (user_id integer, symbol text, open_time timestamp, open float, close float)
                ON COMMIT DROP"""))
            db.execute(text("""INSERT INTO spot_candles_1m VALUES
                (3, 'BTCUSDT', '2025-12-31 23:59', 99, 101),
                (3, 'BTCUSDT', '2026-01-01 00:00', 102, 999),
                (9, 'BTCUSDT', '2025-12-31 23:59', 999, 999)"""))
            day = date(2026, 1, 1)
            params = {"first": day, "last": day, "user_id": 3, "closed_before": datetime(2026, 1, 1)}
            row = db.execute(runtime.DAILY_MARKS, params).mappings().one()
            assert (row["close"], row["exact_close"], row["open"]) == (101, True, None)
            params["closed_before"] += timedelta(minutes=1)
            row = db.execute(runtime.DAILY_MARKS, params).mappings().one()
            assert (row["close"], row["open"]) == (101, 102)
    finally:
        engine.dispose()


def test_testnet_submission_checks_real_clock_after_planning():
    db, exchange = Mock(), Mock()
    account = SimpleNamespace(is_running=True)
    decision = {"boundary": "2020-01-01", "decision_id": "d", "request": True, "execution": {"status": "pending", "side": "BUY", "client_id": "id", "decision_id": "d", "notional": "10"}}
    state = SimpleNamespace(runtime_state={"v12_wa": {"decision": decision}})
    outcome = runtime._finish_testnet(db, account, state, exchange, submit=True)
    assert outcome["status"] == "expired" and account.is_running
    assert not exchange.mock_calls
