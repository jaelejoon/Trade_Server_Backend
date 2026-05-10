from datetime import datetime, timedelta

from app.core.config import CANDLE_GAP_REFILL_LOOKBACK_HOURS
from app.services.binance_service import get_futures_klines
from app.services.candle_service import CandleService


class BinanceCandleSyncService:
    @staticmethod
    def sync_recent_1m_candles(db, user_id: int, symbol: str, limit: int = 1500):
        klines = get_futures_klines(symbol=symbol, interval='1m', limit=limit)
        inserted = CandleService.save_1m_candles(db=db, user_id=user_id, symbol=symbol, candles=klines)
        gap_result = BinanceCandleSyncService.refill_recent_gaps(db=db, user_id=user_id, symbol=symbol)
        return {'fetched': len(klines), 'inserted': inserted, 'gap_refill': gap_result}

    @staticmethod
    def warmup_historical_1m_candles(db, user_id: int, symbol: str, days: int = 60):
        end_time = datetime.utcnow().replace(second=0, microsecond=0)
        start_time = end_time - timedelta(days=days)
        current = start_time
        total_fetched = 0
        total_inserted = 0

        while current < end_time:
            batch_end = min(current + timedelta(minutes=1500), end_time)
            klines = get_futures_klines(
                symbol=symbol,
                interval='1m',
                start_time=int(current.timestamp() * 1000),
                end_time=int(batch_end.timestamp() * 1000),
                limit=1500,
            )
            if not klines:
                break
            inserted = CandleService.save_1m_candles(db=db, user_id=user_id, symbol=symbol, candles=klines)
            total_fetched += len(klines)
            total_inserted += inserted
            last_open_time = klines[-1][0]
            next_current = datetime.utcfromtimestamp(last_open_time / 1000) + timedelta(minutes=1)
            if next_current <= current:
                break
            current = next_current

        gap_result = BinanceCandleSyncService.refill_recent_gaps(db=db, user_id=user_id, symbol=symbol)
        return {'fetched': total_fetched, 'inserted': total_inserted, 'gap_refill': gap_result}

    @staticmethod
    def refill_recent_gaps(db, user_id: int, symbol: str, lookback_hours: int = CANDLE_GAP_REFILL_LOOKBACK_HOURS):
        gaps = CandleService.find_recent_gaps(db=db, user_id=user_id, symbol=symbol, lookback_hours=lookback_hours)
        total_fetched = 0
        total_inserted = 0

        for gap in gaps[:20]:
            klines = get_futures_klines(
                symbol=symbol,
                interval='1m',
                start_time=int(gap['gap_start'].timestamp() * 1000),
                end_time=int((gap['gap_end'] + timedelta(minutes=1)).timestamp() * 1000),
                limit=min(1500, max(1, int(gap['missing_minutes']) + 2)),
            )
            inserted = CandleService.save_1m_candles(db=db, user_id=user_id, symbol=symbol, candles=klines)
            total_fetched += len(klines)
            total_inserted += inserted

        return {'gaps_found': len(gaps), 'refill_fetched': total_fetched, 'refill_inserted': total_inserted}
