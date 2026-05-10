from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import (
    ALLOWED_ORIGINS,
    BINANCE_FUTURES_BASE_URL,
    BINANCE_SPOT_BASE_URL,
    BINANCE_TESTNET,
    DISABLE_DOCS,
    DRY_RUN,
    ENVIRONMENT,
)
from app.core.middleware import SecurityHeadersMiddleware, SimpleRateLimitMiddleware
from app.db.base import Base
from app.db.database import engine
from app.models import *  # noqa: F401,F403
from app.routers import auth, binance, candles, health, strategy, trading
from app.scheduler.bot_scheduler import shutdown_scheduler, start_scheduler


Base.metadata.create_all(bind=engine)


app = FastAPI(
    title="Multi User Binance Trading Server",
    description="사용자별 Binance API Key 기반 V57 자동매매 서버",
    version="0.5.0-stability-security",
    docs_url=None if DISABLE_DOCS else "/docs",
    redoc_url=None if DISABLE_DOCS else "/redoc",
    openapi_url=None if DISABLE_DOCS else "/openapi.json",
)

app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(SimpleRateLimitMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(binance.router)
app.include_router(candles.router)
app.include_router(strategy.router)
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