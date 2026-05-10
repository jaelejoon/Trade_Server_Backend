from sqlalchemy import Column, DateTime, Float, Integer, String, UniqueConstraint

from app.db.base import Base


class Candle1m(Base):
    __tablename__ = "candles_1m"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    symbol = Column(String(50), nullable=False, index=True)
    open_time = Column(DateTime, nullable=False, index=True)

    open = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    volume = Column(Float, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "symbol",
            "open_time",
            name="uq_user_symbol_open_time",
        ),
    )
