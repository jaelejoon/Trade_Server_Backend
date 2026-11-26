from sqlalchemy import Column, DateTime, Integer, String, Text

from app.db.base import Base


class CandleImportJob(Base):
    __tablename__ = "candle_import_jobs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    symbol = Column(String(50), nullable=False, index=True)
    interval = Column(String(20), nullable=False, default="1m")

    status = Column(String(30), nullable=False, default="queued", index=True)
    start_time = Column(DateTime, nullable=False)
    end_time = Column(DateTime, nullable=False)
    cursor_time = Column(DateTime, nullable=True)

    expected_candles = Column(Integer, nullable=False, default=0)
    fetched_count = Column(Integer, nullable=False, default=0)
    inserted_count = Column(Integer, nullable=False, default=0)

    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
