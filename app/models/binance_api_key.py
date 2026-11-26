from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from app.core.security import decrypt_text, mask_sensitive_text, now_utc
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
    enable_withdrawals = Column(Boolean, default=False)
    ip_restricted = Column(Boolean, default=False)

    created_at = Column(DateTime, default=now_utc)

    user = relationship("User", back_populates="api_keys")

    @property
    def masked_api_key(self) -> str:
        try:
            return mask_sensitive_text(decrypt_text(self.api_key_encrypted))
        except Exception:
            return "복호화 실패"
