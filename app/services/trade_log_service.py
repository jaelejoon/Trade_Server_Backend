from typing import Optional

from sqlalchemy.orm import Session

from app.models.trade_log import TradeLog


def create_trade_log(
    db: Session,
    user_id: int,
    api_key_id: Optional[int],
    trading_account_id: Optional[int],
    symbol: str,
    side: str,
    order_type: str,
    qty: float,
    price: Optional[float],
    reduce_only: bool,
    dry_run: bool,
    status_value: str,
    strategy: Optional[str],
    signal_time: Optional[str],
    stop_price: Optional[float],
    take_profit_price: Optional[float],
    raw_response: str,
    executed_qty: Optional[float] = None,
    avg_fill_price: Optional[float] = None,
    entry_order_id: Optional[str] = None,
    stop_order_id: Optional[str] = None,
    take_profit_order_id: Optional[str] = None,
    client_order_id: Optional[str] = None,
    emergency_close_executed: bool = False,
    emergency_close_order_id: Optional[str] = None,
    error_message: Optional[str] = None,
):
    log = TradeLog(
        user_id=user_id,
        api_key_id=api_key_id,
        trading_account_id=trading_account_id,
        symbol=symbol.upper(),
        side=side,
        order_type=order_type,
        qty=qty,
        executed_qty=executed_qty,
        price=price,
        avg_fill_price=avg_fill_price,
        reduce_only=reduce_only,
        dry_run=dry_run,
        status=status_value,
        strategy=strategy,
        signal_time=signal_time,
        stop_price=stop_price,
        take_profit_price=take_profit_price,
        raw_response=raw_response,
        entry_order_id=entry_order_id,
        stop_order_id=stop_order_id,
        take_profit_order_id=take_profit_order_id,
        client_order_id=client_order_id,
        emergency_close_executed=emergency_close_executed,
        emergency_close_order_id=emergency_close_order_id,
        error_message=error_message,
    )

    db.add(log)
    db.commit()
    db.refresh(log)
    return log
