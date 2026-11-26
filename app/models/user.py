from sqlalchemy import Boolean, Column, DateTime, Integer, String
from sqlalchemy.orm import relationship

from app.core.security import now_utc
from app.db.base import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    dry_run = Column(Boolean, default=True, nullable=False)
    is_admin = Column(Boolean, default=False, nullable=False)
    is_prime_admin = Column(Boolean, default=False, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    enable_live_trading = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=now_utc)

    api_keys = relationship("BinanceApiKey", back_populates="user")
    refresh_tokens = relationship("RefreshToken", back_populates="user")
