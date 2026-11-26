from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.core.config import (
    CANDLE_IMPORT_MAX_1M_CANDLES,
    CANDLE_IMPORT_MAX_RUNNING_GLOBAL,
    CANDLE_IMPORT_REQUEST_SLEEP_SECONDS,
)
from app.db.session import SessionLocal
from app.models.candle_import_job import CandleImportJob
from app.services.binance_service import get_futures_klines
from app.services.candle_service import CandleService

_IMPORT_LOCK = threading.Lock()


class CandleImportJobService:
    @staticmethod
    def estimate_1m_candles(start_time: datetime, end_time: datetime) -> int:
        diff_seconds = (end_time - start_time).total_seconds()
        if diff_seconds <= 0:
            return 0
        return int(diff_seconds // 60) + 1

    @staticmethod
    def serialize_job(job: CandleImportJob) -> dict:
        progress_pct = 0.0
        if job.expected_candles > 0:
            progress_pct = min(100.0, round(job.fetched_count / job.expected_candles * 100, 2))

        return {
            "id": job.id,
            "user_id": job.user_id,
            "symbol": job.symbol,
            "interval": job.interval,
            "status": job.status,
            "start_time": job.start_time,
            "end_time": job.end_time,
            "cursor_time": job.cursor_time,
            "expected_candles": job.expected_candles,
            "fetched_count": job.fetched_count,
            "inserted_count": job.inserted_count,
            "progress_pct": progress_pct,
            "error_message": job.error_message,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
        }

    @staticmethod
    def get_running_job(db: Session, user_id: int, symbol: str) -> CandleImportJob | None:
        return (
            db.query(CandleImportJob)
            .filter(CandleImportJob.user_id == user_id)
            .filter(CandleImportJob.symbol == symbol.upper())
            .filter(CandleImportJob.interval == "1m")
            .filter(CandleImportJob.status.in_(["queued", "running"]))
            .order_by(CandleImportJob.id.desc())
            .first()
        )


    @staticmethod
    def count_active_jobs(db: Session) -> int:
        return (
            db.query(CandleImportJob)
            .filter(CandleImportJob.interval == "1m")
            .filter(CandleImportJob.status.in_(["queued", "running"]))
            .count()
        )

    @staticmethod
    def create_job(
        db: Session,
        user_id: int,
        symbol: str,
        start_time: datetime,
        end_time: datetime,
    ) -> CandleImportJob:
        expected_candles = CandleImportJobService.estimate_1m_candles(start_time, end_time)

        if expected_candles <= 0:
            raise ValueError("시작 시간이 종료 시간보다 빠르게 입력되어야 합니다.")

        if expected_candles > CANDLE_IMPORT_MAX_1M_CANDLES:
            raise ValueError(
                f"요청 범위가 너무 큽니다. 예상 1분봉 {expected_candles:,}개, "
                f"최대 {CANDLE_IMPORT_MAX_1M_CANDLES:,}개까지 허용됩니다."
            )

        running_job = CandleImportJobService.get_running_job(db=db, user_id=user_id, symbol=symbol)
        if running_job:
            raise RuntimeError(f"이미 실행 중인 작업이 있습니다. job_id={running_job.id}")

        active_job_count = CandleImportJobService.count_active_jobs(db=db)
        if active_job_count >= CANDLE_IMPORT_MAX_RUNNING_GLOBAL:
            raise RuntimeError(
                f"현재 서버의 캔들 적재 작업 한도({CANDLE_IMPORT_MAX_RUNNING_GLOBAL}개)에 도달했습니다. "
                "진행 중인 작업이 완료된 후 다시 실행하세요."
            )

        now = datetime.utcnow()
        job = CandleImportJob(
            user_id=user_id,
            symbol=symbol.upper(),
            interval="1m",
            status="queued",
            start_time=start_time,
            end_time=end_time,
            cursor_time=start_time,
            expected_candles=expected_candles,
            fetched_count=0,
            inserted_count=0,
            error_message=None,
            created_at=now,
            started_at=None,
            finished_at=None,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job

    @staticmethod
    def start_background_job(job_id: int) -> None:
        thread = threading.Thread(
            target=CandleImportJobService.run_job,
            args=(job_id,),
            daemon=True,
        )
        thread.start()

    @staticmethod
    def run_job(job_id: int) -> None:
        with _IMPORT_LOCK:
            db = SessionLocal()
            try:
                job = db.query(CandleImportJob).filter(CandleImportJob.id == job_id).first()
                if not job:
                    return

                job.status = "running"
                job.started_at = datetime.utcnow()
                job.error_message = None
                db.commit()

                current = job.cursor_time or job.start_time
                end_time = job.end_time
                symbol = job.symbol

                while current < end_time:
                    batch_end = min(current + timedelta(minutes=1500), end_time)

                    klines = get_futures_klines(
                        symbol=symbol,
                        interval="1m",
                        start_time=int(current.timestamp() * 1000),
                        end_time=int(batch_end.timestamp() * 1000),
                        limit=1500,
                    )

                    job = db.query(CandleImportJob).filter(CandleImportJob.id == job_id).first()
                    if not job:
                        return

                    if not klines:
                        current = batch_end + timedelta(minutes=1)
                        job.cursor_time = current
                        db.commit()
                        time.sleep(CANDLE_IMPORT_REQUEST_SLEEP_SECONDS)
                        continue

                    inserted = CandleService.save_1m_candles(
                        db=db,
                        user_id=job.user_id,
                        symbol=symbol,
                        candles=klines,
                    )

                    last_open_time_ms = int(klines[-1][0])
                    next_current = datetime.utcfromtimestamp(last_open_time_ms / 1000) + timedelta(minutes=1)
                    if next_current <= current:
                        next_current = batch_end + timedelta(minutes=1)

                    job = db.query(CandleImportJob).filter(CandleImportJob.id == job_id).first()
                    if not job:
                        return

                    job.fetched_count += len(klines)
                    job.inserted_count += inserted
                    job.cursor_time = next_current
                    db.commit()

                    current = next_current
                    time.sleep(CANDLE_IMPORT_REQUEST_SLEEP_SECONDS)

                job = db.query(CandleImportJob).filter(CandleImportJob.id == job_id).first()
                if job:
                    job.status = "success"
                    job.cursor_time = end_time
                    job.finished_at = datetime.utcnow()
                    db.commit()

            except Exception as exc:
                db.rollback()
                job = db.query(CandleImportJob).filter(CandleImportJob.id == job_id).first()
                if job:
                    job.status = "failed"
                    job.error_message = str(exc)
                    job.finished_at = datetime.utcnow()
                    db.commit()
            finally:
                db.close()
