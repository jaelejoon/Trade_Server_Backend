from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import AwareDatetime
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.jwt import get_current_user
from app.db.session import get_db
from app.models.user import User

router = APIRouter(prefix="/data")
SYMBOL = "BTCUSDT"
MINUTE = timedelta(minutes=1)
INTERVAL_MINUTES = {"1m": 1, "15m": 15, "1h": 60, "4h": 240}

INTEGRITY_QUERY = text("""
    WITH timestamps AS (
        SELECT open_time, count(*) AS rows_at_time
        FROM candles_1m
        WHERE user_id = :user_id AND symbol = :symbol AND open_time < :closed_before
        GROUP BY open_time
    ), aligned AS (
        SELECT open_time, lag(open_time) OVER (ORDER BY open_time) AS previous_time
        FROM timestamps
        WHERE open_time = date_trunc('minute', open_time)
    ), coverage AS (
        SELECT min(open_time) AS start, max(open_time) AS "end", count(*) AS candle_count,
               count(*) FILTER (WHERE open_time - previous_time > INTERVAL '1 minute')
                   AS gap_count,
               coalesce(sum(extract(epoch FROM open_time - previous_time)::bigint / 60 - 1), 0)::bigint
                   AS missing_minutes
        FROM aligned
    ), quality AS (
        SELECT count(*) FILTER (WHERE rows_at_time > 1) AS duplicate_timestamps,
               coalesce(sum(rows_at_time - 1), 0)::bigint AS duplicate_rows,
               coalesce(sum(rows_at_time) FILTER (
                   WHERE open_time <> date_trunc('minute', open_time)
               ), 0)::bigint AS misaligned_rows
        FROM timestamps
    )
    SELECT * FROM coverage CROSS JOIN quality
""")

CANDLES_QUERY = text("""
    SELECT date_bin(:stride, open_time, TIMESTAMP '1970-01-01') AS bucket,
           (array_agg(open ORDER BY open_time))[1] AS open,
           max(high) AS high, min(low) AS low,
           (array_agg(close ORDER BY open_time DESC))[1] AS close,
           sum(volume) AS volume
    FROM candles_1m
    WHERE user_id = :user_id AND symbol = :symbol
      AND open_time >= :lower_bound AND open_time < :upper_bound
      AND open_time = date_trunc('minute', open_time)
    GROUP BY bucket
    HAVING count(*) = :minutes AND count(DISTINCT open_time) = :minutes
    ORDER BY bucket
    LIMIT :fetch_count
""")


def _utc_naive(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _utc_aware(value: datetime | None) -> datetime | None:
    return None if value is None else value.replace(tzinfo=timezone.utc)


def _closed_before() -> datetime:
    return _utc_naive(datetime.now(timezone.utc)).replace(second=0, microsecond=0)


def _inspect(db: Session, user_id: int) -> dict:
    row = db.execute(INTEGRITY_QUERY, {
        "user_id": user_id, "symbol": SYMBOL, "closed_before": _closed_before(),
    }).mappings().one()
    return {
        **row,
        "symbol": SYMBOL,
        "interval": "1m",
        "start": _utc_aware(row["start"]),
        "end": _utc_aware(row["end"]),
    }


@router.get("/status")
def get_data_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    status = _inspect(db, current_user.id)
    return {key: status[key] for key in (
        "symbol", "interval", "start", "end", "candle_count", "gap_count",
    )}


@router.get("/integrity")
def get_data_integrity(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _inspect(db, current_user.id)


@router.get("/candles")
def get_data_candles(
    start: AwareDatetime,
    end: AwareDatetime,
    interval: Literal["1m", "15m", "1h", "4h"] = "1m",
    limit: int = Query(default=500, ge=1, le=1000),
    cursor: AwareDatetime | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    start_utc = _utc_naive(start)
    end_utc = _utc_naive(end)
    if start_utc >= end_utc:
        raise HTTPException(status_code=422, detail="end는 start보다 뒤여야 합니다.")

    minutes = INTERVAL_MINUTES[interval]
    stride = minutes * MINUTE
    lower_bound = start_utc
    if cursor is not None:
        cursor_utc = _utc_naive(cursor)
        if not start_utc <= cursor_utc < end_utc:
            raise HTTPException(status_code=422, detail="cursor가 조회 범위를 벗어났습니다.")
        if (cursor_utc - datetime(1970, 1, 1)) % stride:
            raise HTTPException(status_code=422, detail="cursor를 봉 시작 시각에 맞춰 주세요.")
        lower_bound = cursor_utc + min(stride, end_utc - cursor_utc)

    rows = db.execute(CANDLES_QUERY, {
        "user_id": current_user.id,
        "symbol": SYMBOL,
        "stride": stride,
        "minutes": minutes,
        "lower_bound": lower_bound,
        "upper_bound": min(end_utc, _closed_before()).replace(second=0, microsecond=0),
        "fetch_count": limit + 1,
    }).mappings().all()
    items = []
    for row in rows[:limit]:
        candle = dict(row)
        candle["open_time"] = _utc_aware(candle.pop("bucket"))
        items.append(candle)

    return {
        "symbol": SYMBOL, "interval": interval, "items": items,
        "next_cursor": items[-1]["open_time"] if len(rows) > limit else None,
    }
