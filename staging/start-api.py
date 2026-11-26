import hmac
import os
import sys


def main():
    sys.path.insert(0, "/app")
    expected_database = os.environ.get("DATABASE_URL", "")
    try:
        from sqlalchemy.engine import make_url

        from app.core import config

        database = make_url(expected_database)

        safe = (
            config.ENVIRONMENT == "staging"
            and config.BINANCE_TESTNET is True
            and not hasattr(config, "BINANCE_FUTURES_BASE_URL")
            and not os.environ.get("BINANCE_FUTURES_BASE_URL")
            and config.BINANCE_SPOT_BASE_URL == "https://testnet.binance.vision"
            and config.DRY_RUN is True
            and config.ENABLE_LIVE_TRADING is False
            and isinstance(config.ENABLE_TRADING_SCHEDULER, bool)
            and os.environ.get("ENABLE_TRADING_SCHEDULER", "false") in {"false", "true"}
            and database.drivername == "postgresql+psycopg2"
            and database.host == "staging-db"
            and database.port == 5432
            and database.username == database.database == "trade_staging"
            and not database.query
            and hmac.compare_digest(config.DATABASE_URL, expected_database)
        )
    except Exception:
        safe = False

    if not safe:
        raise SystemExit("검증 API 시작 거부: 테스트넷·모의 실행·검증 DB 설정을 확인하세요.")

    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, proxy_headers=False)


if __name__ == "__main__":
    main()
