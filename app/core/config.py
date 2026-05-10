import os

from dotenv import load_dotenv

load_dotenv(dotenv_path='.env', override=True)

JWT_SECRET_KEY = os.getenv('JWT_SECRET_KEY', 'dev-jwt-secret')
ENCRYPTION_KEY = os.getenv('ENCRYPTION_KEY')
DATABASE_URL = os.getenv('DATABASE_URL', 'sqlite:///./app.db')

BINANCE_TESTNET = os.getenv('BINANCE_TESTNET', 'false').lower() == 'true'

BINANCE_FUTURES_BASE_URL = os.getenv(
    'BINANCE_FUTURES_BASE_URL',
    'https://testnet.binancefuture.com'
    if BINANCE_TESTNET
    else 'https://fapi.binance.com',
)

BINANCE_SPOT_BASE_URL = os.getenv(
    'BINANCE_SPOT_BASE_URL',
    'https://api.binance.com',
)

DRY_RUN = os.getenv('DRY_RUN', 'true').lower() == 'true'

ENABLE_LIVE_TRADING = (
    os.getenv('ENABLE_LIVE_TRADING', 'false').lower() == 'true'
)

ACCESS_TOKEN_EXPIRE_MINUTES = int(
    os.getenv('ACCESS_TOKEN_EXPIRE_MINUTES', str(60 * 24))
)

ALGORITHM = 'HS256'

CANDLE_WARMUP_DAYS = int(
    os.getenv('CANDLE_WARMUP_DAYS', '60')
)

MIN_STRATEGY_1M_CANDLES = int(
    os.getenv('MIN_STRATEGY_1M_CANDLES', '43200')
)

STRATEGY_LOAD_1M_LIMIT = int(
    os.getenv('STRATEGY_LOAD_1M_LIMIT', '120000')
)

BOT_INTERVAL_SECONDS = int(
    os.getenv('BOT_INTERVAL_SECONDS', '60')
)

# =========================
# 운영 보안 옵션
# =========================

ENVIRONMENT = os.getenv('ENVIRONMENT', 'dev').lower()

DISABLE_DOCS = (
    os.getenv('DISABLE_DOCS', 'false').lower() == 'true'
)

ALLOWED_ORIGINS_RAW = os.getenv(
    'ALLOWED_ORIGINS',
    '',
)

RATE_LIMIT_PER_MINUTE = int(
    os.getenv('RATE_LIMIT_PER_MINUTE', '120')
)


def build_allowed_origins() -> list[str]:
    origins = []

    if ALLOWED_ORIGINS_RAW:
        origins.extend(
            [
                item.strip()
                for item in ALLOWED_ORIGINS_RAW.split(',')
                if item.strip()
            ]
        )

    if ENVIRONMENT == 'dev':
        origins.extend(
            [
                'http://localhost:3000',
                'http://127.0.0.1:3000',
                'http://localhost:5173',
                'http://127.0.0.1:5173',
            ]
        )

    return list(dict.fromkeys(origins))


ALLOWED_ORIGINS = build_allowed_origins()

# =========================
# Binance 요청 안정화
# =========================

BINANCE_REQUEST_TIMEOUT_SECONDS = int(
    os.getenv('BINANCE_REQUEST_TIMEOUT_SECONDS', '10')
)

BINANCE_MAX_RETRIES = int(
    os.getenv('BINANCE_MAX_RETRIES', '3')
)

BINANCE_RETRY_BASE_SLEEP_SECONDS = float(
    os.getenv('BINANCE_RETRY_BASE_SLEEP_SECONDS', '0.5')
)

# =========================
# 캔들 안정화
# =========================

EXCLUDE_INCOMPLETE_1M_CANDLE = (
    os.getenv(
        'EXCLUDE_INCOMPLETE_1M_CANDLE',
        'true',
    ).lower()
    == 'true'
)

CANDLE_GAP_REFILL_LOOKBACK_HOURS = int(
    os.getenv('CANDLE_GAP_REFILL_LOOKBACK_HOURS', '24')
)

SERVER_TIME_DRIFT_LIMIT_MS = int(
    os.getenv('SERVER_TIME_DRIFT_LIMIT_MS', '1000')
)

# =========================
# 실주문 안전장치
# =========================

AUTO_CLOSE_ON_BRACKET_FAILURE = (
    os.getenv(
        'AUTO_CLOSE_ON_BRACKET_FAILURE',
        'true',
    ).lower()
    == 'true'
)

if not ENCRYPTION_KEY:
    raise RuntimeError(
        'ENCRYPTION_KEY가 없습니다. .env에 Fernet 키를 넣어주세요.'
    )