from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String

from app.core.security import now_utc
from app.db.base import Base


class TradingAccount(Base):
    __tablename__ = "trading_accounts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    api_key_id = Column(Integer, ForeignKey("binance_api_keys.id"), nullable=False)

    market_type = Column(String(50), default="USDT_FUTURES")
    symbol = Column(String(50), default="BTCUSDT")
    leverage = Column(Integer, default=2)

    max_risk_ratio = Column(Float, default=0.03)
    max_daily_loss_ratio = Column(Float, default=0.03)

    is_running = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)

    created_at = Column(DateTime, default=now_utc)