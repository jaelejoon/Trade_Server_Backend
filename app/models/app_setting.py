from sqlalchemy import Column, DateTime, String

from app.core.security import now_utc
from app.db.base import Base


class AppSetting(Base):
    __tablename__ = "app_settings"

    key = Column(String(100), primary_key=True, index=True)
    value = Column(String(255), nullable=False)
    updated_at = Column(DateTime, default=now_utc, onupdate=now_utc)
