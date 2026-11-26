import hmac
import os
import sys


def main():
    sys.path.insert(0, "/app")
    expected_database = os.environ["DATABASE_URL"]
    try:
        from app.core import config

        safe = (
            config.ENVIRONMENT == "staging"
            and config.BINANCE_TESTNET is True
            and config.BINANCE_TESTNET_FUTURES_URL == "https://demo-fapi.binance.com"
            and config.BINANCE_FUTURES_BASE_URL == "https://demo-fapi.binance.com"
            and config.BINANCE_SPOT_BASE_URL == "https://testnet.binance.vision"
            and config.DRY_RUN is True
            and config.ENABLE_LIVE_TRADING is False
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
