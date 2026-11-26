from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text

from app.core.security import now_utc
from app.db.base import Base


class LatestSignalState(Base):
    __tablename__ = "latest_signal_states"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    trading_account_id = Column(Integer, ForeignKey("trading_accounts.id"), unique=True, nullable=False, index=True)
    symbol = Column(String(50), nullable=False, index=True)

    status = Column(String(80), nullable=True, index=True)
    message = Column(String(255), nullable=True)
    signal_time = Column(String(100), nullable=True, index=True)
    strategy = Column(String(100), nullable=True, index=True)
    side = Column(String(20), nullable=True, index=True)

    signal_json = Column(Text, nullable=True)
    tick_json = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)

    calculated_at = Column(DateTime, default=now_utc, index=True)
    updated_at = Column(DateTime, default=now_utc, index=True)
