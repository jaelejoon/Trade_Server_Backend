from app.core.config import MIN_STRATEGY_1M_CANDLES, STRATEGY_LOAD_1M_LIMIT
from app.services.binance_candle_sync import BinanceCandleSyncService
from app.services.candle_service import CandleService
from app.strategy.strategy_v57 import get_latest_v57_signal, signal_to_dict


def get_v57_signal_from_db_candles(db, user_id: int, symbol: str, sync_recent: bool = True):
    symbol = symbol.upper()

    sync_result = None
    if sync_recent:
        sync_result = BinanceCandleSyncService.sync_recent_1m_candles(
            db=db,
            user_id=user_id,
            symbol=symbol,
            limit=1500,
        )

    candle_count = CandleService.get_candle_count(
        db=db,
        user_id=user_id,
        symbol=symbol,
    )

    if candle_count < MIN_STRATEGY_1M_CANDLES:
        return {
            "status": "error",
            "message": "INSUFFICIENT_CANDLE_HISTORY",
            "symbol": symbol,
            "current_count": candle_count,
            "required_count": MIN_STRATEGY_1M_CANDLES,
            "sync_result": sync_result,
        }

    raw_df = CandleService.load_recent_1m_dataframe(
        db=db,
        user_id=user_id,
        symbol=symbol,
        limit=STRATEGY_LOAD_1M_LIMIT,
    )

    if raw_df.empty:
        return {
            "status": "error",
            "message": "NO_CANDLE_DATA",
            "symbol": symbol,
            "sync_result": sync_result,
        }

    signal = get_latest_v57_signal(raw_df)
    signal_dict = signal_to_dict(signal)

    if not signal.has_signal:
        return {
            "status": "no_signal",
            "message": signal_dict.get("reason") or "V57 신호 없음",
            "symbol": symbol,
            "signal": signal_dict,
            "sync_result": sync_result,
        }

    return {
        "status": "signal",
        "message": "V57 신호 발생",
        "symbol": symbol,
        "signal": signal_dict,
        "sync_result": sync_result,
    }
