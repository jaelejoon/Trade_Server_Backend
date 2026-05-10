from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.config import CANDLE_GAP_REFILL_LOOKBACK_HOURS, CANDLE_WARMUP_DAYS
from app.core.jwt import get_current_user
from app.db.session import get_db
from app.models.user import User
from app.services.binance_candle_sync import BinanceCandleSyncService
from app.services.candle_service import CandleService

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
