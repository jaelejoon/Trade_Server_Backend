import os

from dotenv import load_dotenv

load_dotenv(dotenv_path=".env", override=True)

# =========================
# 기본 서버 설정
# =========================

JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "").strip()
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
ENVIRONMENT = os.getenv("ENVIRONMENT", "dev").lower()
ALGORITHM = "HS256"

if not ENCRYPTION_KEY:
    raise RuntimeError("ENCRYPTION_KEY가 없습니다. .env에 Fernet 키를 넣어주세요.")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL이 없습니다. .env에 PostgreSQL DATABASE_URL을 넣어주세요.")

if DATABASE_URL.startswith("sqlite"):
    raise RuntimeError("SQLite는 더 이상 사용하지 않습니다. DATABASE_URL을 postgresql://... 로 설정해주세요.")

if not (
    DATABASE_URL.startswith("postgresql://")
    or DATABASE_URL.startswith("postgresql+psycopg2://")
):
    raise RuntimeError("DATABASE_URL은 PostgreSQL 주소여야 합니다. 예: postgresql://postgres:postgres@localhost:5432/trade")

if len(JWT_SECRET_KEY.encode()) < 32:
    raise RuntimeError("JWT_SECRET_KEY는 32바이트 이상이어야 합니다.")

# =========================
# CORS / Swagger
# =========================

DISABLE_DOCS = os.getenv("DISABLE_DOCS", "false").lower() == "true"
ALLOWED_ORIGINS_RAW = os.getenv("ALLOWED_ORIGINS", "")


def build_allowed_origins() -> list[str]:
    origins: list[str] = []

    if ALLOWED_ORIGINS_RAW:
        origins.extend(
            item.strip()
            for item in ALLOWED_ORIGINS_RAW.split(",")
            if item.strip()
        )

    if ENVIRONMENT == "dev":
        origins.extend(
            [
                "https://localhost",
                "http://localhost",
                "http://localhost:3000",
                "http://127.0.0.1:3000",
                "http://localhost:5173",
                "http://127.0.0.1:5173",
            ]
        )

    return list(dict.fromkeys(origins))


ALLOWED_ORIGINS = build_allowed_origins()

if "*" in ALLOWED_ORIGINS:
    raise RuntimeError("ALLOWED_ORIGINS에는 와일드카드를 사용할 수 없습니다.")

# =========================
# HTTPS / 프록시
# =========================

REQUIRE_HTTPS_AUTH = os.getenv("REQUIRE_HTTPS_AUTH", "true").lower() == "true"
TRUST_PROXY_HEADERS = os.getenv("TRUST_PROXY_HEADERS", "false").lower() == "true"

# =========================
# JWT / Cookie 인증
# =========================

ACCESS_TOKEN_EXPIRE_MINUTES = int(
    os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", str(60 * 24))
)

AUTH_COOKIE_NAME = os.getenv("AUTH_COOKIE_NAME", "access_token")
REFRESH_COOKIE_NAME = os.getenv("REFRESH_COOKIE_NAME", "refresh_token")
REFRESH_TOKEN_EXPIRE_DAYS = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", "7"))

# Capacitor 앱 origin은 https://localhost 이고,
# 백엔드는 ngrok https 도메인이므로 크로스 사이트 쿠키가 필요하다.
# 따라서 앱 테스트 기준 기본값은 Secure=true, SameSite=None 이다.
AUTH_COOKIE_SECURE = os.getenv("AUTH_COOKIE_SECURE", "true").lower() == "true"
AUTH_COOKIE_SAMESITE = os.getenv("AUTH_COOKIE_SAMESITE", "none").lower()

if AUTH_COOKIE_SAMESITE not in {"lax", "strict", "none"}:
    raise RuntimeError("AUTH_COOKIE_SAMESITE는 lax, strict, none 중 하나여야 합니다.")

if AUTH_COOKIE_SAMESITE == "none" and not AUTH_COOKIE_SECURE:
    raise RuntimeError("SameSite=None 쿠키는 AUTH_COOKIE_SECURE=true가 필요합니다.")

# =========================
# 로그인 보안
# =========================

LOGIN_FAILURE_LIMIT = int(os.getenv("LOGIN_FAILURE_LIMIT", "5"))
LOGIN_LOCK_MINUTES = int(os.getenv("LOGIN_LOCK_MINUTES", "15"))
LOGIN_FAILURE_WINDOW_MINUTES = int(os.getenv("LOGIN_FAILURE_WINDOW_MINUTES", "15"))

# =========================
# Rate Limit / 요청 제한
# =========================

RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "120"))
AUTH_RATE_LIMIT_PER_MINUTE = int(os.getenv("AUTH_RATE_LIMIT_PER_MINUTE", "10"))
WRITE_RATE_LIMIT_PER_MINUTE = int(os.getenv("WRITE_RATE_LIMIT_PER_MINUTE", "30"))
RATE_LIMIT_BLOCK_SECONDS = int(os.getenv("RATE_LIMIT_BLOCK_SECONDS", "60"))
MAX_REQUEST_BODY_BYTES = int(os.getenv("MAX_REQUEST_BODY_BYTES", "1048576"))

# =========================
# Binance
# =========================

BINANCE_TESTNET = os.getenv("BINANCE_TESTNET", "false").lower() == "true"

BINANCE_TESTNET_FUTURES_URL = "https://demo-fapi.binance.com"
BINANCE_PRODUCTION_FUTURES_URL = "https://fapi.binance.com"
_expected_futures_url = BINANCE_TESTNET_FUTURES_URL if BINANCE_TESTNET else BINANCE_PRODUCTION_FUTURES_URL
BINANCE_FUTURES_BASE_URL = os.getenv("BINANCE_FUTURES_BASE_URL", _expected_futures_url).rstrip("/")
if BINANCE_FUTURES_BASE_URL != _expected_futures_url:
    raise RuntimeError("Binance 선물 URL이 선택한 거래 환경과 일치하지 않습니다.")

_expected_spot_url = "https://testnet.binance.vision" if BINANCE_TESTNET else "https://api.binance.com"
BINANCE_SPOT_BASE_URL = os.getenv("BINANCE_SPOT_BASE_URL", _expected_spot_url).rstrip("/")
if BINANCE_SPOT_BASE_URL != _expected_spot_url:
    raise RuntimeError("Binance 현물 URL이 선택한 거래 환경과 일치하지 않습니다.")

DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
ENABLE_LIVE_TRADING = os.getenv("ENABLE_LIVE_TRADING", "false").lower() == "true"

# =========================
# 캔들 / 전략
# =========================

CANDLE_WARMUP_DAYS = int(os.getenv("CANDLE_WARMUP_DAYS", "60"))
MIN_STRATEGY_1M_CANDLES = int(os.getenv("MIN_STRATEGY_1M_CANDLES", "43200"))
STRATEGY_LOAD_1M_LIMIT = int(os.getenv("STRATEGY_LOAD_1M_LIMIT", "120000"))
ENABLE_TRADING_SCHEDULER = os.getenv("ENABLE_TRADING_SCHEDULER", "false").lower() == "true"
BOT_INTERVAL_SECONDS = int(os.getenv("BOT_INTERVAL_SECONDS", "60"))

STRATEGY_CALCULATION_MODE = os.getenv("STRATEGY_CALCULATION_MODE", "closed_candle")
STRATEGY_CALCULATION_GRACE_SECONDS = int(
    os.getenv("STRATEGY_CALCULATION_GRACE_SECONDS", "10")
)

EXCLUDE_INCOMPLETE_1M_CANDLE = (
    os.getenv("EXCLUDE_INCOMPLETE_1M_CANDLE", "true").lower() == "true"
)
CANDLE_GAP_REFILL_LOOKBACK_HOURS = int(
    os.getenv("CANDLE_GAP_REFILL_LOOKBACK_HOURS", "24")
)
CANDLE_IMPORT_MAX_1M_CANDLES = int(
    os.getenv("CANDLE_IMPORT_MAX_1M_CANDLES", "4000000")
)
CANDLE_IMPORT_REQUEST_SLEEP_SECONDS = float(
    os.getenv("CANDLE_IMPORT_REQUEST_SLEEP_SECONDS", "0.08")
)
SERVER_TIME_DRIFT_LIMIT_MS = int(os.getenv("SERVER_TIME_DRIFT_LIMIT_MS", "1000"))

# =========================
# Heavy Job 제한
# =========================

BACKTEST_MAX_RUNNING_PER_USER = int(os.getenv("BACKTEST_MAX_RUNNING_PER_USER", "1"))
BACKTEST_MAX_RUNNING_GLOBAL = int(os.getenv("BACKTEST_MAX_RUNNING_GLOBAL", "2"))
CANDLE_IMPORT_MAX_RUNNING_GLOBAL = int(os.getenv("CANDLE_IMPORT_MAX_RUNNING_GLOBAL", "1"))

# =========================
# Binance API 안정성
# =========================

BINANCE_REQUEST_TIMEOUT_SECONDS = int(
    os.getenv("BINANCE_REQUEST_TIMEOUT_SECONDS", "10")
)
BINANCE_MAX_RETRIES = int(os.getenv("BINANCE_MAX_RETRIES", "3"))
BINANCE_RETRY_BASE_SLEEP_SECONDS = float(
    os.getenv("BINANCE_RETRY_BASE_SLEEP_SECONDS", "0.5")
)
AUTO_CLOSE_ON_BRACKET_FAILURE = (
    os.getenv("AUTO_CLOSE_ON_BRACKET_FAILURE", "true").lower() == "true"
)