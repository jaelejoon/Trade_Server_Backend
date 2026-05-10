from datetime import datetime, timedelta

import pandas as pd
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import EXCLUDE_INCOMPLETE_1M_CANDLE
from app.models.candle import Candle1m


class CandleService:
    @staticmethod
    def save_1m_candles(db: Session, user_id: int, symbol: str, candles: list):
        if not candles:
            return 0

        rows = []
        symbol = symbol.upper()
        now_utc = datetime.utcnow().replace(second=0, microsecond=0)

        for k in candles:
            open_time = pd.to_datetime(k[0], unit='ms').to_pydatetime()

            # 진행 중인 1분봉은 백테스트와 실시간 괴리를 만들 수 있으므로 저장 제외
            if EXCLUDE_INCOMPLETE_1M_CANDLE and open_time >= now_utc:
                continue

            rows.append({
                'user_id': user_id,
                'symbol': symbol,
                'open_time': open_time,
                'open': float(k[1]),
                'high': float(k[2]),
                'low': float(k[3]),
                'close': float(k[4]),
                'volume': float(k[5]),
            })

        if not rows:
            return 0

        stmt = insert(Candle1m).values(rows)
        stmt = stmt.on_conflict_do_nothing(index_elements=['user_id', 'symbol', 'open_time'])
        result = db.execute(stmt)
        db.commit()
        return result.rowcount or 0

    @staticmethod
    def load_recent_1m_dataframe(db: Session, user_id: int, symbol: str, limit: int = 100000):
        rows = (
            db.query(Candle1m)
            .filter(Candle1m.user_id == user_id)
            .filter(Candle1m.symbol == symbol.upper())
            .order_by(Candle1m.open_time.desc())
            .limit(limit)
            .all()
        )

        if not rows:
            return pd.DataFrame()

        rows.reverse()
        data = []
        for r in rows:
            data.append({
                'timestamp': r.open_time,
                'open': r.open,
                'high': r.high,
                'low': r.low,
                'close': r.close,
                'volume': r.volume,
            })
        return pd.DataFrame(data)

    @staticmethod
    def get_candle_count(db: Session, user_id: int, symbol: str):
        return (
            db.query(Candle1m)
            .filter(Candle1m.user_id == user_id)
            .filter(Candle1m.symbol == symbol.upper())
            .count()
        )

    @staticmethod
    def get_candle_range(db: Session, user_id: int, symbol: str):
        first = (
            db.query(Candle1m)
            .filter(Candle1m.user_id == user_id)
            .filter(Candle1m.symbol == symbol.upper())
            .order_by(Candle1m.open_time.asc())
            .first()
        )
        last = (
            db.query(Candle1m)
            .filter(Candle1m.user_id == user_id)
            .filter(Candle1m.symbol == symbol.upper())
            .order_by(Candle1m.open_time.desc())
            .first()
        )
        return {
            'first_open_time': first.open_time if first else None,
            'last_open_time': last.open_time if last else None,
        }

    @staticmethod
    def find_recent_gaps(db: Session, user_id: int, symbol: str, lookback_hours: int = 24):
        since = datetime.utcnow() - timedelta(hours=lookback_hours)
        rows = (
            db.query(Candle1m.open_time)
            .filter(Candle1m.user_id == user_id)
            .filter(Candle1m.symbol == symbol.upper())
            .filter(Candle1m.open_time >= since)
            .order_by(Candle1m.open_time.asc())
            .all()
        )

        gaps = []
        previous = None
        for row in rows:
            current = row[0]
            if previous is not None:
                delta_minutes = int((current - previous).total_seconds() // 60)
                if delta_minutes > 1:
                    gaps.append({
                        'gap_start': previous + timedelta(minutes=1),
                        'gap_end': current - timedelta(minutes=1),
                        'missing_minutes': delta_minutes - 1,
                    })
            previous = current

        return gaps
