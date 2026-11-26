import argparse
from datetime import datetime

from app.db.session import SessionLocal
from app.services.candle_import_job_service import CandleImportJobService


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)


def main() -> None:
    parser = argparse.ArgumentParser(description="Binance 선물 1분봉을 PostgreSQL candles_1m에 적재합니다.")
    parser.add_argument("--user-id", type=int, required=True, help="저장할 사용자 ID")
    parser.add_argument("--symbol", default="BTCUSDT", help="심볼. 기본값 BTCUSDT")
    parser.add_argument("--start", default="2020-01-01T00:00:00", help="시작 일시. 예: 2020-01-01T00:00:00")
    parser.add_argument("--end", default=None, help="종료 일시. 생략 시 현재 UTC")
    args = parser.parse_args()

    start_time = parse_dt(args.start)
    end_time = parse_dt(args.end) if args.end else datetime.utcnow().replace(second=0, microsecond=0)

    db = SessionLocal()
    try:
        job = CandleImportJobService.create_job(
            db=db,
            user_id=args.user_id,
            symbol=args.symbol,
            start_time=start_time,
            end_time=end_time,
        )
        print(f"작업 생성: job_id={job.id}, symbol={job.symbol}, expected={job.expected_candles:,}")
    finally:
        db.close()

    CandleImportJobService.run_job(job.id)

    db = SessionLocal()
    try:
        job = db.query(type(job)).filter(type(job).id == job.id).first()
        print(CandleImportJobService.serialize_job(job))
    finally:
        db.close()


if __name__ == "__main__":
    main()
