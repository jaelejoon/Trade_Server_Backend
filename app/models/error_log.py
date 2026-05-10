from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text

from app.core.security import now_utc
from app.db.base import Base


class ErrorLog(Base):
    __tablename__ = 'error_logs'

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=True, index=True)
    trading_account_id = Column(Integer, ForeignKey('trading_accounts.id'), nullable=True, index=True)
    level = Column(String(30), default='ERROR', index=True)
    source = Column(String(100), nullable=False)
    message = Column(Text, nullable=False)
    raw_data = Column(Text, nullable=True)
    created_at = Column(DateTime, default=now_utc, index=True)
