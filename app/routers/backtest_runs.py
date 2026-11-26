import logging
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from math import isfinite
from typing import Literal

import pandas as pd
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.orm import Session

from app.core.config import BACKTEST_MAX_RUNNING_GLOBAL, BACKTEST_MAX_RUNNING_PER_USER
from app.core.jwt import get_current_user
from app.db.session import SessionLocal, get_db
from app.models.backtest_run import backtest_runs as runs
from app.models.candle import Candle1m
from app.models.user import User
from app.services.backtest_engine import (
    CANDLE_COLUMNS,
    ENGINE_VERSION,
    STRATEGIES,
    BacktestInputError,
    candle_checksum,
    replay,
    validate_candles,
)
from app.services.strategy_1x_regimes import replay_regime
from app.services.binance_service import binance_public_get


router = APIRouter(prefix="/runs")
logger = logging.getLogger(__name__)
ACTIVE = ("queued", "running")
RUN_FIELDS = [column for column in runs.c if column.name not in {"user_id", "result"}]


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    strategy_id: str = Field(min_length=1, max_length=80)
    strategy_version: str = Field(default="1", min_length=1, max_length=20)
    symbol: Literal["BTCUSDT"] = "BTCUSDT"
    start: AwareDatetime
    end: AwareDatetime
    initial_equity_usdt: float = Field(ge=0.01, le=1_000_000_000)
    leverage: int = Field(default=1, strict=True, ge=1, le=2)
    fee_bps: float = Field(default=5, ge=0, le=100)
    slippage_bps: float = Field(default=0, ge=0, le=100)
    funding: Literal["historical", "disabled"] = "historical"

    @field_validator("start", "end", mode="before")
    @classmethod
    def parse_iso_datetime(cls, value):
        if not isinstance(value, str):
            raise ValueError("UTC 오프셋이 있는 ISO 8601 시각을 입력하세요.")
        return datetime.fromisoformat(value)

    @model_validator(mode="after")
    def check_period_and_strategy(self):
        self.start = self.start.astimezone(timezone.utc)
        self.end = self.end.astimezone(timezone.utc)
        closed_through = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        if (
            self.start.year < 2019
            or self.start != self.start.replace(second=0, microsecond=0)
            or self.end != self.end.replace(second=0, microsecond=0)
            or self.end > closed_through
            or not timedelta(0) < self.end - self.start <= timedelta(days=31)
        ):
            raise ValueError("종료된 분봉만 사용합니다. 시작·종료 시각은 분 경계이며 기간은 1분~31일이어야 합니다.")
        strategy = STRATEGIES.get(self.strategy_id)
        if strategy is None or strategy.version != self.strategy_version:
            raise ValueError("지원하지 않는 전략 또는 전략 버전입니다.")
        if strategy.one_x_only and self.leverage != 1:
            raise ValueError("1배 전용 전략은 레버리지 1만 허용합니다.")
        return self


def load_funding(body):
    if body.funding == "disabled":
        return {}, "disabled"

    start_ms = int(body.start.timestamp() * 1000)
    end_ms = int(body.end.timestamp() * 1000)
    try:
        payments = binance_public_get("https://fapi.binance.com", "/fapi/v1/fundingRate", {
            "symbol": body.symbol, "startTime": start_ms, "endTime": end_ms - 1, "limit": 1000,
        })
        if not isinstance(payments, list) or len(payments) >= 1000:
            raise ValueError("Invalid funding response")
        funding = {}
        for payment in payments:
            time_ms = int(payment["fundingTime"])
            time = pd.Timestamp(time_ms, unit="ms").floor("min")
            price, rate = float(payment["markPrice"]), float(payment["fundingRate"])
            if (
                not start_ms <= time_ms < end_ms
                or time in funding
                or not isfinite(price) or price <= 0
                or not isfinite(rate) or abs(rate) > 1
            ):
                raise ValueError("Invalid funding record")
            funding[time] = (price, rate)
    except (HTTPException, KeyError, TypeError, ValueError) as exc:
        raise BacktestInputError("과거 펀딩 데이터 조회 또는 검증에 실패했습니다.") from exc

    if not funding:
        return {}, "unavailable"
    boundaries = [pd.Timestamp(body.start).tz_localize(None), *sorted(funding),
                  pd.Timestamp(body.end).tz_localize(None)]
    if any(right - left > pd.Timedelta(hours=8) for left, right in zip(boundaries, boundaries[1:])):
        raise BacktestInputError("과거 펀딩 데이터에 8시간을 초과하는 공백이 있습니다.")
    return funding, "applied"


def simulate(db, user_id, body):
    strategy = STRATEGIES[body.strategy_id]
    start = pd.Timestamp(body.start).tz_localize(None)
    end = pd.Timestamp(body.end).tz_localize(None)
    warmup_start = start.floor("4h") - strategy.warmup
    columns = [getattr(Candle1m, name) for name in CANDLE_COLUMNS]
    query = select(Candle1m.open_time, *columns).where(
        Candle1m.user_id == user_id,
        Candle1m.symbol == body.symbol,
        Candle1m.open_time >= warmup_start,
        Candle1m.open_time < end,
    ).order_by(Candle1m.open_time)
    candles = pd.DataFrame(db.execute(query).all(), columns=["open_time", *CANDLE_COLUMNS])
    candles = candles.set_index(pd.DatetimeIndex(candles.pop("open_time"), name="open_time"))
    # One ordered SQL statement supplies the exact snapshot used by the simulation.
    db.rollback()
    validate_candles(candles, warmup_start, end)
    funding, funding_status = load_funding(body)
    if strategy.regime is not None:
        result = replay_regime(
            candles.loc[start:], strategy.regime.decisions(candles), strategy.regime.exposure,
            body.initial_equity_usdt, body.fee_bps, body.slippage_bps, funding,
        )
    else:
        result = replay(
            candles.loc[start:], strategy.signals(candles), body.initial_equity_usdt,
            body.leverage, body.fee_bps, body.slippage_bps, funding,
            loss_limits=strategy.loss_limits,
        )
    funding_hash = sha256(repr(sorted(funding.items())).encode()).hexdigest() if funding else None
    return {
        **result,
        "engine_version": ENGINE_VERSION,
        "execution_model": ("monthly_long_cash_next_open_1m" if strategy.regime
                            else "next_open_1m_stop_first_fixed_maintenance"),
        "candle_count": len(candles),
        "candle_sha256": candle_checksum(candles),
        "funding": {
            "status": funding_status,
            "label": "반영" if funding_status == "applied" else "미반영",
            "record_count": len(funding),
            "sha256": funding_hash,
            "net_cost_usdt": sum(trade["funding_cost_usdt"] for trade in result["trades"]),
        },
    }


def expire_jobs(db):
    db.execute(update(runs).where(
        runs.c.status.in_(ACTIVE),
        runs.c.updated_at < func.now() - text("interval '15 minutes'"),
    ).values(status="failed", updated_at=func.now(), error_message="백테스트 실행 시간이 초과되었습니다."))


def execute_run(run_id):
    with SessionLocal() as db:
        job = db.execute(update(runs).where(
            runs.c.id == run_id, runs.c.status == "queued",
        ).values(status="running", updated_at=func.now()).returning(
            runs.c.user_id, runs.c.request,
        )).one_or_none()
        db.commit()
        if job is None:
            return

        try:
            result = simulate(db, job.user_id, RunRequest.model_validate(job.request))
            values = {"status": "succeeded", "result": result}
        except BacktestInputError as exc:
            values = {"status": "failed", "error_message": str(exc)}
        except Exception as exc:
            logger.warning("Backtest %s failed (%s)", run_id, type(exc).__name__)
            values = {"status": "failed", "error_message": "백테스트를 완료하지 못했습니다."}

        db.rollback()
        db.execute(update(runs).where(
            runs.c.id == run_id, runs.c.status == "running",
        ).values(**values, updated_at=func.now()))
        db.commit()


@router.post("", status_code=202)
def create_run(
    body: RunRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # Serialize admission across server processes until the pending row commits.
    db.execute(text("SELECT pg_advisory_xact_lock(716042371)"))
    expire_jobs(db)
    active = select(func.count()).select_from(runs).where(runs.c.status.in_(ACTIVE))
    if (
        db.scalar(active.where(runs.c.user_id == user.id)) >= BACKTEST_MAX_RUNNING_PER_USER
        or db.scalar(active) >= BACKTEST_MAX_RUNNING_GLOBAL
    ):
        db.commit()
        raise HTTPException(409, "백테스트 실행 한도에 도달했습니다.")
    run_id = db.scalar(insert(runs).values(
        user_id=user.id, status="queued", request=body.model_dump(mode="json"),
    ).returning(runs.c.id))
    db.commit()
    background_tasks.add_task(execute_run, run_id)
    return {"id": run_id, "status": "queued"}


@router.get("")
def list_runs(
    before_id: int | None = Query(None, ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    expire_jobs(db)
    query = select(*RUN_FIELDS).where(runs.c.user_id == user.id)
    if before_id is not None:
        query = query.where(runs.c.id < before_id)
    rows = db.execute(query.order_by(runs.c.id.desc()).limit(limit + 1)).mappings().all()
    db.commit()
    return {"items": [dict(row) for row in rows[:limit]],
            "next_before_id": rows[limit - 1]["id"] if len(rows) > limit else None}


@router.get("/{run_id}")
def get_run(
    run_id: int,
    trade_offset: int = Query(0, ge=0),
    equity_offset: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=1000),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    expire_jobs(db)
    row = db.execute(select(runs).where(
        runs.c.id == run_id, runs.c.user_id == user.id,
    )).mappings().one_or_none()
    db.commit()
    if row is None:
        raise HTTPException(404, "백테스트 실행을 찾을 수 없습니다.")
    response = dict(row)
    del response["user_id"]
    result = response["result"]
    if result:
        for name, offset in (("trades", trade_offset), ("equity_curve", equity_offset)):
            items = result[name]
            result[name] = {
                "items": items[offset:offset + limit],
                "total": len(items),
                "next_offset": offset + limit if offset + limit < len(items) else None,
            }
    return response
