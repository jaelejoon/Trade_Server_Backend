from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text

from app.core.security import now_utc
from app.db.base import Base


class TradeLog(Base):
    __tablename__ = 'trade_logs'

    id = Column(Integer, primary_key=True, index=True)

    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    api_key_id = Column(Integer, ForeignKey('binance_api_keys.id'), nullable=True)
    trading_account_id = Column(Integer, ForeignKey('trading_accounts.id'), nullable=True, index=True)

    symbol = Column(String(50), nullable=False, index=True)
    side = Column(String(20), nullable=False)
    order_type = Column(String(50), default='MARKET')

    qty = Column(Float, nullable=False)
    executed_qty = Column(Float, nullable=True)
    price = Column(Float, nullable=True)
    avg_fill_price = Column(Float, nullable=True)

    reduce_only = Column(Boolean, default=False)
    dry_run = Column(Boolean, default=True)

    status = Column(String(80), default='CREATED', index=True)
    pnl = Column(Float, nullable=True)
    fee = Column(Float, nullable=True)

    strategy = Column(String(100), nullable=True)
    signal_time = Column(String(100), nullable=True)
    stop_price = Column(Float, nullable=True)
    take_profit_price = Column(Float, nullable=True)

    entry_order_id = Column(String(100), nullable=True)
    stop_order_id = Column(String(100), nullable=True)
    take_profit_order_id = Column(String(100), nullable=True)
    client_order_id = Column(String(120), nullable=True)

    emergency_close_executed = Column(Boolean, default=False)
    emergency_close_order_id = Column(String(100), nullable=True)

    raw_response = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=now_utc)
