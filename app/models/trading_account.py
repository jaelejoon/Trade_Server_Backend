from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String

from app.core.security import now_utc
from app.core.trading_limits import MAX_LEVERAGE, TRADING_SYMBOL
from app.db.base import Base


class TradingAccount(Base):
    __tablename__ = "trading_accounts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    api_key_id = Column(Integer, ForeignKey("binance_api_keys.id"), nullable=False)

    market_type = Column(String(50), default="USDT_FUTURES")
    symbol = Column(String(50), default=TRADING_SYMBOL)
    leverage = Column(Integer, default=MAX_LEVERAGE)

    max_risk_ratio = Column(Float, default=0.03)
    max_daily_loss_ratio = Column(Float, default=0.03)

    dry_run = Column(Boolean, default=True, nullable=False)

    is_running = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)

    created_at = Column(DateTime, default=now_utc)