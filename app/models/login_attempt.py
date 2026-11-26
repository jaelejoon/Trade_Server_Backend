from sqlalchemy import Column, DateTime, Integer, String

from app.core.security import now_utc
from app.db.base import Base


class LoginAttempt(Base):
    __tablename__ = 'login_attempts'

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), nullable=False, index=True)
    ip_address = Column(String(100), nullable=False, index=True)
    failed_count = Column(Integer, default=0, nullable=False)
    locked_until = Column(DateTime, nullable=True, index=True)
    first_failed_at = Column(DateTime, nullable=True)
    last_failed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=now_utc)
    updated_at = Column(DateTime, default=now_utc, onupdate=now_utc)
