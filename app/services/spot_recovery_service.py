"""Recover only BTC backed by this bot's confirmed fills."""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.models.spot_fill import SpotFill
from app.services.spot_service import recovery_quantity


def record_order_fills(db, account_id, confirmed_order_id, verified_trades):
    """Persist exchange trades fetched for a journaled bot order."""
    for trade in verified_trades:
        if trade["orderId"] != confirmed_order_id:
            raise ValueError("조회한 체결이 봇 주문과 일치하지 않습니다.")
        stmt = insert(SpotFill).values(
            trading_account_id=account_id,
            trade_id=trade["id"],
            order_id=confirmed_order_id,
            side="BUY" if trade["isBuyer"] else "SELL",
            quantity=trade["qty"],
            commission=trade["commission"],
            commission_asset=trade["commissionAsset"],
        )
        db.execute(stmt.on_conflict_do_nothing())


def inspect_recovery(db, account_id, exchange):
    rows = db.execute(
        select(
            SpotFill.trade_id, SpotFill.side, SpotFill.quantity,
            SpotFill.commission, SpotFill.commission_asset,
        )
        .where(SpotFill.trading_account_id == account_id)
        .order_by(SpotFill.trade_id)
    ).mappings().all()
    balances = exchange.account()["balances"]
    btc = next((item for item in balances if item["asset"] == "BTC"), {"free": "0", "locked": "0"})
    recovery = recovery_quantity(rows, btc["free"], btc["locked"])
    orders = [item for item in exchange.open_order_lists() if item["symbol"] == "BTCUSDT"]
    return recovery, orders
