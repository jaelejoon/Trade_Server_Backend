from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.config import CANDLE_GAP_REFILL_LOOKBACK_HOURS, CANDLE_WARMUP_DAYS
from app.core.jwt import get_current_user
from app.db.session import get_db
from app.models.candle import Candle1m
from app.models.candle_import_job import CandleImportJob
from app.models.user import User
from app.routers.admin import require_admin
from app.services.binance_candle_sync import BinanceCandleSyncService
from app.services.candle_service import CandleService
from app.services.candle_import_job_service import CandleImportJobService

router = APIRouter(tags=['candles'])


@router.post('/candles/1m/sync/{symbol}')
def sync_1m_candles(symbol: str, days: int = CANDLE_WARMUP_DAYS, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    sync_result = BinanceCandleSyncService.warmup_historical_1m_candles(db=db, user_id=current_user.id, symbol=symbol, days=days)
    count = CandleService.get_candle_count(db=db, user_id=current_user.id, symbol=symbol)
    candle_range = CandleService.get_candle_range(db=db, user_id=current_user.id, symbol=symbol)
    gaps = CandleService.find_recent_gaps(db=db, user_id=current_user.id, symbol=symbol, lookback_hours=CANDLE_GAP_REFILL_LOOKBACK_HOURS)
    return {'status': 'success', 'symbol': symbol.upper(), 'days': days, 'sync_result': sync_result, 'stored_1m_candles': count, 'range': candle_range, 'recent_gap_count': len(gaps), 'recent_gaps_sample': gaps[:10]}


@router.post('/candles/1m/refill-gaps/{symbol}')
def refill_gaps(symbol: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    result = BinanceCandleSyncService.refill_recent_gaps(db=db, user_id=current_user.id, symbol=symbol)
    return {'status': 'success', 'symbol': symbol.upper(), 'result': result}


@router.get('/candles/1m/status/{symbol}')
def candle_status(symbol: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    count = CandleService.get_candle_count(db=db, user_id=current_user.id, symbol=symbol)
    candle_range = CandleService.get_candle_range(db=db, user_id=current_user.id, symbol=symbol)
    gaps = CandleService.find_recent_gaps(db=db, user_id=current_user.id, symbol=symbol, lookback_hours=CANDLE_GAP_REFILL_LOOKBACK_HOURS)
    return {'symbol': symbol.upper(), 'stored_1m_candles': count, 'range': candle_range, 'recent_gap_count': len(gaps), 'recent_gaps_sample': gaps[:10]}



def _parse_datetime_param(value: str | None):
    if not value:
        return None

    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).replace(tzinfo=None)
    except ValueError:
        raise HTTPException(status_code=400, detail='날짜/시간 형식이 올바르지 않습니다. 예: 2026-05-19T09:00')


@router.get('/candles/1m/{symbol}')
def list_1m_candles(
        symbol: str,
        start: str | None = None,
        end: str | None = None,
        limit: int = 500,
        db: Session = Depends(get_db),
        current_user: User = Depends(get_current_user),
):
    if limit < 1 or limit > 5000:
        raise HTTPException(status_code=400, detail='limit은 1 이상 5000 이하로 입력하세요.')

    start_dt = _parse_datetime_param(start)
    end_dt = _parse_datetime_param(end)

    query = (
        db.query(Candle1m)
        .filter(Candle1m.user_id == current_user.id)
        .filter(Candle1m.symbol == symbol.upper())
    )

    if start_dt:
        query = query.filter(Candle1m.open_time >= start_dt)
    if end_dt:
        query = query.filter(Candle1m.open_time <= end_dt)

    total_count = query.count()
    rows = query.order_by(Candle1m.open_time.asc()).limit(limit).all()

    return {
        'status': 'ok',
        'symbol': symbol.upper(),
        'start': start_dt,
        'end': end_dt,
        'limit': limit,
        'total_count': total_count,
        'returned_count': len(rows),
        'items': [
            {
                'id': row.id,
                'open_time': row.open_time,
                'open': row.open,
                'high': row.high,
                'low': row.low,
                'close': row.close,
                'volume': row.volume,
            }
            for row in rows
        ],
    }

#POST /candles/1m/import-jobs/BTCUSDT?start=2020-01-01T00:00:00
@router.post('/candles/1m/import-jobs/{symbol}')
def create_1m_import_job(
        symbol: str,

        start: str = Query(
            default='2020-01-01T00:00:00',
            description='1분봉 수집 시작 시각 (UTC)',
            example='2020-01-01T00:00:00',
        ),

        end: str | None = Query(
            default=None,
            description='1분봉 수집 종료 시각 (UTC). 비우면 현재 UTC 시각 사용',
            example='2026-05-27T00:00:00',
        ),

        db: Session = Depends(get_db),
        current_user: User = Depends(require_admin),
):
    start_dt = _parse_datetime_param(start)

    end_dt = (
        _parse_datetime_param(end)
        if end
        else datetime.utcnow().replace(second=0, microsecond=0)
    )

    try:
        job = CandleImportJobService.create_job(
            db=db,
            user_id=current_user.id,
            symbol=symbol,
            start_time=start_dt,
            end_time=end_dt,
        )

    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    CandleImportJobService.start_background_job(job.id)

    return {
        'status': 'queued',
        'message': '1분봉 데이터 적재 작업을 시작했습니다.',
        'job': CandleImportJobService.serialize_job(job),
    }


@router.get('/candles/1m/import-jobs/{job_id}')
def get_1m_import_job(
        job_id: int,
        db: Session = Depends(get_db),
        current_user: User = Depends(get_current_user),
):
    job = (
        db.query(CandleImportJob)
        .filter(CandleImportJob.id == job_id)
        .filter(CandleImportJob.user_id == current_user.id)
        .first()
    )

    if not job:
        raise HTTPException(status_code=404, detail='작업을 찾을 수 없습니다.')

    return {
        'status': 'ok',
        'job': CandleImportJobService.serialize_job(job),
    }


@router.get('/candles/1m/import-jobs/latest/{symbol}')
def get_latest_1m_import_job(
        symbol: str,
        db: Session = Depends(get_db),
        current_user: User = Depends(get_current_user),
):
    job = (
        db.query(CandleImportJob)
        .filter(CandleImportJob.user_id == current_user.id)
        .filter(CandleImportJob.symbol == symbol.upper())
        .filter(CandleImportJob.interval == '1m')
        .order_by(CandleImportJob.id.desc())
        .first()
    )

    if not job:
        return {
            'status': 'empty',
            'job': None,
        }

    return {
        'status': 'ok',
        'job': CandleImportJobService.serialize_job(job),
    }
