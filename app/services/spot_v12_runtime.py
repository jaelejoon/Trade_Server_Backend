"""Persist V12 decisions and simulate bounded orders without exchange credentials."""

from hashlib import sha256
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_DOWN

from sqlalchemy import select, text

from app.core.config import DRY_RUN, ENABLE_SPOT_TESTNET_ORDERS, SPOT_TESTNET_MAX_ORDER_USDT
from app.models.bot_signal_state import BotSignalState
from app.models.trading_account import TradingAccount
from app.models.user import User
from app.services.account_service import get_api_key_owned_by_user
from app.services.binance_service import get_decrypted_api_keys
from app.services.spot_lifecycle import SpotLifecycle
from app.services.spot_recovery_service import inspect_recovery
from app.services.spot_service import SpotExchange
from app.services.spot_v12 import DAY, advance, quarter, rebalance

FIRST_DAY = date(2017, 9, 1)
FEE = Decimal(".001")
SIMULATION_CASH = Decimal("10000")

DAILY_MARKS = text("""
SELECT boundary.ts::date AS day, previous.close,
       previous.open_time = boundary.ts - interval '1 minute' AS exact_close,
       opening.open
FROM generate_series(CAST(:first AS timestamp), CAST(:last AS timestamp), interval '1 day') AS boundary(ts)
LEFT JOIN LATERAL (
    SELECT open_time, close FROM spot_candles_1m
    WHERE user_id = :user_id AND symbol = 'BTCUSDT'
      AND open_time >= boundary.ts - interval '1 day' AND open_time < boundary.ts
    ORDER BY open_time DESC LIMIT 1
) previous ON true
LEFT JOIN spot_candles_1m opening ON opening.user_id = :user_id AND opening.symbol = 'BTCUSDT'
     AND opening.open_time = boundary.ts AND opening.open_time < :closed_before
ORDER BY boundary.ts
""")


def next_boundary(day):
    return next(day + timedelta(days=offset) for offset in range(1, 8)
                if (day + timedelta(days=offset)).weekday() == 0 or quarter(day + timedelta(days=offset)))


def paper_intent(book, result, price):
    cash = Decimal(book["cash"])
    owned = Decimal(book["quantity"])
    price = Decimal(str(price))
    side, quantity = rebalance(
        cash, owned, owned, price, Decimal(str(result["target"])), FEE, SPOT_TESTNET_MAX_ORDER_USDT,
    )
    lot = Decimal(".00001")
    quantity = (quantity / lot).to_integral_value(rounding=ROUND_DOWN) * lot
    notional = quantity * price
    if quantity <= 0 or notional < 5:
        return {"status": "below_order_minimum"}
    fee = notional * FEE
    if side == "BUY":
        book.update(cash=str(cash - notional - fee), quantity=str(owned + quantity))
    else:
        book.update(cash=str(cash + notional - fee), quantity=str(owned - quantity))
    return {"status": "simulated", "side": side, "quantity": str(quantity),
            "price": str(price), "notional": str(notional), "fee": str(fee)}


def run_v12_tick(db, account, *, now=None, recover_only=False):
    now = now or datetime.now(timezone.utc)
    boundary = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today = boundary.date()
    # Wait until the exact boundary one-minute bar can be finalized.
    through = today if now >= boundary + timedelta(minutes=1) else today - DAY
    params = {"key": account.api_key_id}
    with db.get_bind().connect() as lock:
        if not lock.execute(text("SELECT pg_try_advisory_lock(571126, :key)"), params).scalar():
            return {"status": "busy"}
        try:
            return _calculate(db, account, now, boundary, today, through, recover_only)
        finally:
            lock.execute(text("SELECT pg_advisory_unlock(571126, :key)"), params)


def _exchange(db, account):
    user = db.get(User, account.user_id)
    if not user or not user.is_active or not user.is_admin:
        return None
    key = get_api_key_owned_by_user(db, account.user_id, account.api_key_id)
    return SpotExchange(*get_decrypted_api_keys(key))


def _testnet_plan(db, account, exchange, decision, journal):
    if any(journal.get(key) for key in ("halted", "buy", "sell", "oco")):
        return {"status": "blocked", "reason": "existing_spot_journal"}
    recovery, orders = inspect_recovery(db, account.id, exchange)
    if orders or (recovery["halt"] and recovery["balance"] != 0):
        return {"status": "blocked", "reason": "unconfirmed_inventory_or_open_orders"}
    if recovery["halt"] and db.scalar(text(
        "SELECT 1 FROM spot_fills WHERE trading_account_id = :account_id LIMIT 1"
    ), {"account_id": account.id}) is not None:
        return {"status": "blocked", "reason": "unconfirmed_inventory"}
    cash = next((Decimal(row["free"]) for row in exchange.account()["balances"]
                 if row["asset"] == "USDT"), Decimal(0))
    price = exchange.price()
    if price <= 0:
        return {"status": "blocked", "reason": "invalid_price"}
    side, amount = rebalance(cash, recovery["owned"], recovery["quantity"], price,
                             Decimal(str(decision["target"])), FEE, SPOT_TESTNET_MAX_ORDER_USDT)
    if amount <= 0:
        return {"status": "below_order_minimum"}
    try:
        quantity, _ = exchange._normalize(amount, exchange._reference_prices(), market=True)
        quote = (quantity * price).quantize(Decimal(".01"), rounding=ROUND_DOWN)
        if side == "BUY":
            exchange._normalize(quote / price, exchange._reference_prices(), market=True)
    except ValueError:
        return {"status": "below_order_minimum"}
    return {"status": "pending", "side": side, "quantity": str(quantity), "quote": str(quote),
            "client_id": "v12-" + sha256(decision["decision_id"].encode()).hexdigest()[:28]}


def _finish_testnet(db, account, state, exchange, *, submit):
    stored = state.runtime_state["v12_wa"]
    decision = stored["decision"]
    execution = dict(decision["execution"])
    expiry = datetime.combine(date.fromisoformat(decision["boundary"]), time.min, tzinfo=timezone.utc)
    if submit and datetime.now(timezone.utc) >= expiry + timedelta(minutes=5):
        execution["status"] = "expired"
    else:
        try:
            if not submit:
                order = exchange.lookup(execution["client_id"])
            elif execution["side"] == "BUY":
                order = exchange.market_buy_quote(Decimal(execution["quote"]), execution["client_id"])
            else:
                order = exchange.order("SELL", Decimal(execution["quantity"]), execution["client_id"])
            if order is None or not SpotLifecycle(db, account, state, exchange).ingest(order, execution["side"]):
                raise ValueError("체결 확인 필요")
            execution["status"] = "confirmed"
            execution["exchange_status"] = order["status"]
        except Exception:
            db.rollback()
            account.is_running = False
            execution["status"] = "unconfirmed"
    outcome = dict(decision, execution=execution)
    history = {**stored.get("history", {}), outcome["boundary"]: outcome}
    state.runtime_state = {**state.runtime_state,
                           "v12_wa": {**stored, "decision": outcome, "history": history}}
    db.commit()
    return {"status": execution["status"], "decision": outcome}


def _calculate(db, account, now, boundary, today, through, recover_only):
    db.refresh(account)
    if not account.is_running or not account.is_active or account.market_type != "SPOT":
        db.rollback()
        return {"status": "paused"}
    state = db.scalar(select(BotSignalState).where(BotSignalState.trading_account_id == account.id))
    runtime = dict(state.runtime_state or {}) if state else {}
    if runtime.get("spot", {}).get("signal_time") == "spot-testnet-check":
        db.rollback()
        return {"status": "reserved_for_testnet_check"}
    stored = dict(runtime.get("v12_wa") or {})
    history = dict(stored.get("history") or {})
    decision = stored.get("decision") or {}
    if decision.get("execution", {}).get("status") == "pending":
        exchange = _exchange(db, account) if ENABLE_SPOT_TESTNET_ORDERS else None
        if exchange is not None:
            return _finish_testnet(db, account, state, exchange, submit=False)
        db.rollback()
        return {"status": "blocked", "reason": "pending_testnet_order"}
    if decision.get("execution", {}).get("status") == "unconfirmed":
        db.rollback()
        return {"status": "blocked", "reason": "unconfirmed_testnet_order"}
    if recover_only:
        db.rollback()
        return {"status": "paused"}
    model = stored.get("model")
    first = date.fromisoformat(model["boundary"]) + DAY if model else FIRST_DAY
    if first > through:
        db.rollback()
        return {"status": "up_to_date"}
    rows = db.execute(DAILY_MARKS, {
        "first": first, "last": through, "user_id": account.user_id,
        "closed_before": now.replace(second=0, microsecond=0, tzinfo=None),
    }).mappings()
    recent = stored.get("decision")
    paper = dict(stored.get("paper") or {"cash": str(SIMULATION_CASH), "quantity": "0"})
    exchange = None
    for row in rows:
        day = row["day"]
        eligible = day == today and now < boundary + timedelta(minutes=5)
        if eligible and (quarter(day) or day.weekday() == 0) and (
            row["open"] is None or not row["exact_close"]
        ):
            break
        model, decision = advance(model, day, row["close"], row["exact_close"], row["open"])
        if decision is None:
            continue
        recent = decision
        decision["decision_id"] = f"{account.id}:{decision['decision_id']}"
        decision["calculated_at"] = now.isoformat()
        decision["execution"] = {"status": "no_change"}
        if decision["request"]:
            if not eligible or not decision["data_valid"] or not row["exact_close"] or row["open"] is None:
                decision["execution"] = {"status": "deferred"}
            elif ENABLE_SPOT_TESTNET_ORDERS:
                exchange = _exchange(db, account)
                decision["execution"] = (_testnet_plan(db, account, exchange, decision, runtime.get("spot", {}))
                                         if exchange else {"status": "blocked"})
            elif DRY_RUN:
                decision["execution"] = paper_intent(paper, decision, row["open"])
            else:
                decision["execution"] = {"status": "blocked"}
        history[day.isoformat()] = decision
    if model is not None:
        runtime["v12_wa"] = {"model": model, "decision": recent, "paper": paper, "history": history}
        if state is None:
            state = BotSignalState(trading_account_id=account.id)
            db.add(state)
        state.runtime_state = runtime
    db.commit()
    if exchange is not None and recent["execution"]["status"] == "pending":
        return _finish_testnet(db, account, state, exchange, submit=True)
    return {"status": "calculated", "decision": recent}


def strategy_status(db, account_id, now):
    account = db.get(TradingAccount, account_id)
    if account is None or account.market_type != "SPOT":
        return None
    row = db.scalar(select(BotSignalState).where(BotSignalState.trading_account_id == account_id))
    stored = (row.runtime_state or {}).get("v12_wa", {}) if row else {}
    decision = stored.get("decision")
    scheduled = datetime.combine(next_boundary(now.date()), time.min, tzinfo=timezone.utc)
    today = datetime.combine(now.date(), time.min, tzinfo=timezone.utc)
    if (quarter(now.date()) or now.weekday() == 0) and now < today + timedelta(minutes=5):
        if not decision or decision["boundary"] != now.date().isoformat():
            scheduled = today
    return {"account_id": account_id, "mode": decision["mode"] if decision else "V12",
            "target": decision["target"] if decision else None,
            "calculated_at": decision["calculated_at"] if decision else None,
            "changed_at": decision["changed_at"] if decision else None,
            "next_calculation_at": scheduled, "decision": decision}
