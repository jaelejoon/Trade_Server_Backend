from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from app.core.security import now_utc
from app.db.base import Base


class BinanceApiKey(Base):
    __tablename__ = "binance_api_keys"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    api_key_encrypted = Column(Text, nullable=False)
    secret_key_encrypted = Column(Text, nullable=False)

    label = Column(String(100), default="default")
    is_active = Column(Boolean, default=True)

    enable_reading = Column(Boolean, default=False)
    enable_futures = Column(Boolean, default=False)
    enable_withdrawals = Column(Boolean, default=False)
    ip_restricted = Column(Boolean, default=False)

    created_at = Column(DateTime, default=now_utc)

    user = relationship("User", back_populates="api_keys")
