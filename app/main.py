import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import (
    ALLOWED_ORIGINS,
    BINANCE_FUTURES_BASE_URL,
    BINANCE_SPOT_BASE_URL,
    BINANCE_TESTNET,
    DISABLE_DOCS,
    DRY_RUN,
    ENVIRONMENT,
)
from app.core.middleware import HttpsAuthMiddleware, RequestSizeLimitMiddleware, SecurityHeadersMiddleware, SimpleRateLimitMiddleware
from app.db.base import Base
from app.db.database import engine
from app.db.migrations import ensure_runtime_schema
from app.models import *  # noqa: F401,F403
from app.routers import admin, auth, backtests, binance, candles, health, strategy, strategy_management, trading
from app.scheduler.bot_scheduler import shutdown_scheduler, start_scheduler


Base.metadata.create_all(bind=engine)
ensure_runtime_schema()


app = FastAPI(
    title="Multi User Binance Trading Server",
    description="사용자별 Binance API Key 기반 V57 자동매매 서버",
    version="0.5.0-stability-security",
    docs_url=None if DISABLE_DOCS else "/docs",
    redoc_url=None if DISABLE_DOCS else "/redoc",
    openapi_url=None if DISABLE_DOCS else "/openapi.json",
)

app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(HttpsAuthMiddleware)
app.add_middleware(RequestSizeLimitMiddleware)
app.add_middleware(SimpleRateLimitMiddleware)

@app.exception_handler(RequestValidationError)
async def invalid_request(_request: Request, _exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"detail": "요청 형식이 올바르지 않습니다."})


@app.middleware("http")
async def handle_unexpected_error(request: Request, call_next):
    try:
        return await call_next(request)
    except Exception as exc:
        logging.getLogger(__name__).error("요청 처리 실패 (%s)", type(exc).__name__)
        return JSONResponse(status_code=500, content={"detail": "요청 처리 중 오류가 발생했습니다."})


app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(binance.router)
app.include_router(candles.router)
app.include_router(strategy_management.router)
app.include_router(strategy.router)
app.include_router(backtests.router)
app.include_router(trading.router)
app.include_router(health.router)


@app.get("/")
def root():
    return {
        "status": "ok",
        "service": "multi-user-binance-server",
        "strategy": "V57_LONG_SAFE_PUSH",
        "docs": None if DISABLE_DOCS else "/docs",
        "environment": ENVIRONMENT,
        "dry_run": DRY_RUN,
        "binance_testnet": BINANCE_TESTNET,
        "futures_base_url": BINANCE_FUTURES_BASE_URL,
        "spot_base_url": BINANCE_SPOT_BASE_URL,
        "allowed_origins": ALLOWED_ORIGINS,
    }


@app.on_event("startup")
def on_startup():
    recovered = start_scheduler()
    print(f"[startup] recovered running trading account jobs: {recovered}")


@app.on_event("shutdown")
def on_shutdown():
    shutdown_scheduler()