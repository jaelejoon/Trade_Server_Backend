from sqlalchemy import Column, DateTime, ForeignKey, Integer, String

from app.core.security import now_utc
from app.db.base import Base


class BotSignalState(Base):
    __tablename__ = "bot_signal_states"

    id = Column(Integer, primary_key=True, index=True)
    trading_account_id = Column(Integer, ForeignKey("trading_accounts.id"), unique=True, nullable=False)
    last_signal_time = Column(String(100), nullable=True)
    last_strategy = Column(String(100), nullable=True)
    updated_at = Column(DateTime, default=now_utc)
