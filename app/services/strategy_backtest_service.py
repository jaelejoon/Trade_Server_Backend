from __future__ import annotations

import contextlib
import io
import math
from datetime import datetime
from typing import Any

import pandas as pd
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core.config import BACKTEST_MAX_RUNNING_GLOBAL, BACKTEST_MAX_RUNNING_PER_USER
from app.db.session import SessionLocal
from app.models.candle import Candle1m
from app.models.trading_account import TradingAccount
from app.models.trading_strategy import StrategyBacktestRun, TradingStrategy, UserStrategySetting
from app.services.trading_strategy_service import get_effective_strategy_for_user
from app.services.binance_service import get_funding_rate_history
from app.strategy.strategy_v57 import (
    V57ConfigOverride,
    backtest,
    build_signal_events,
    calculate_stats,
    prepare_data,
)

MAX_BACKTEST_CANDLES = 3_000_000
MIN_BACKTEST_CANDLES = 5_000


def parse_datetime_param(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        raise HTTPException(status_code=400, detail="날짜/시간 형식이 올바르지 않습니다. 예: 2026-05-19T09:00")


def clean_number(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    if math.isinf(result) or math.isnan(result):
        return 0.0
    return result


def normalize_max_candles(max_candles: int) -> int:
    if max_candles < MIN_BACKTEST_CANDLES:
        return MIN_BACKTEST_CANDLES
    if max_candles > MAX_BACKTEST_CANDLES:
        return MAX_BACKTEST_CANDLES
    return max_candles


def load_backtest_candles(
    db: Session,
    user_id: int,
    symbol: str,
    start: datetime | None,
    end: datetime | None,
    max_candles: int,
) -> pd.DataFrame:
    query = (
        db.query(Candle1m)
        .filter(Candle1m.user_id == user_id)
        .filter(Candle1m.symbol == symbol.upper())
    )

    if start is not None:
        query = query.filter(Candle1m.open_time >= start)
    if end is not None:
        query = query.filter(Candle1m.open_time <= end)

    rows = query.order_by(Candle1m.open_time.desc()).limit(normalize_max_candles(max_candles)).all()
    if not rows:
        return pd.DataFrame()

    rows.reverse()
    return pd.DataFrame([
        {
            "timestamp": row.open_time,
            "open": row.open,
            "high": row.high,
            "low": row.low,
            "close": row.close,
            "volume": row.volume,
        }
        for row in rows
    ])


def serialize_backtest_run(run: StrategyBacktestRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "user_id": run.user_id,
        "trading_account_id": run.trading_account_id,
        "strategy_id": run.strategy_id,
        "setting_id": run.setting_id,
        "symbol": run.symbol,
        "start_time": run.start_time,
        "end_time": run.end_time,
        "source_candle_count": run.source_candle_count,
        "signal_count": run.signal_count,
        "trade_count": run.trade_count,
        "final_equity": run.final_equity,
        "return_pct": run.return_pct,
        "mdd": run.mdd,
        "win_rate": run.win_rate,
        "pf": run.pf,
        "liquidation_count": run.liquidation_count,
        "config_snapshot": run.config_snapshot or {},
        "result_summary": run.result_summary or {},
        "diagnostics": run.diagnostics,
        "status": run.status,
        "error_message": run.error_message,
        "created_at": run.created_at,
        "strategy": {
            "id": run.strategy.id,
            "code": run.strategy.code,
            "name": run.strategy.name,
            "version": run.strategy.version,
        } if run.strategy else None,
    }



def count_active_backtest_jobs(db: Session, user_id: int | None = None) -> int:
    query = db.query(StrategyBacktestRun).filter(StrategyBacktestRun.status.in_(["queued", "running"]))
    if user_id is not None:
        query = query.filter(StrategyBacktestRun.user_id == user_id)
    return query.count()


def validate_backtest_job_capacity(db: Session, user_id: int) -> None:
    user_active_count = count_active_backtest_jobs(db=db, user_id=user_id)
    if user_active_count >= BACKTEST_MAX_RUNNING_PER_USER:
        raise HTTPException(
            status_code=409,
            detail=(
                "이미 실행 대기/진행 중인 백테스트가 있습니다. "
                "완료 후 다시 실행하세요."
            ),
        )

    global_active_count = count_active_backtest_jobs(db=db)
    if global_active_count >= BACKTEST_MAX_RUNNING_GLOBAL:
        raise HTTPException(
            status_code=409,
            detail=(
                f"현재 서버의 백테스트 실행 한도({BACKTEST_MAX_RUNNING_GLOBAL}개)에 도달했습니다. "
                "다른 백테스트가 완료된 후 다시 실행하세요."
            ),
        )

def create_strategy_backtest_job(
    db: Session,
    user_id: int,
    symbol: str,
    trading_account_id: int | None,
    start: str | None,
    end: str | None,
    max_candles: int,
) -> StrategyBacktestRun:
    symbol = symbol.upper().strip()
    start_dt = parse_datetime_param(start)
    end_dt = parse_datetime_param(end)
    max_candles = normalize_max_candles(max_candles)

    validate_backtest_job_capacity(db=db, user_id=user_id)

    if start_dt and end_dt and start_dt >= end_dt:
        raise HTTPException(status_code=400, detail="시작일은 종료일보다 빨라야 합니다.")

    if trading_account_id is not None:
        account = (
            db.query(TradingAccount)
            .filter(
                TradingAccount.id == trading_account_id,
                TradingAccount.user_id == user_id,
                TradingAccount.is_active == True,
            )
            .first()
        )
        if account is None:
            raise HTTPException(status_code=404, detail="자동매매 계정을 찾을 수 없습니다.")
        symbol = account.symbol.upper()

    strategy, effective_config, setting = get_effective_strategy_for_user(
        db=db,
        user_id=user_id,
        trading_account_id=trading_account_id,
    )

    if strategy.engine_key != "V57_LONG_SAFE_PUSH":
        raise HTTPException(status_code=400, detail="현재 백테스트는 V57 엔진 전략만 지원합니다.")

    run = StrategyBacktestRun(
        user_id=user_id,
        trading_account_id=trading_account_id,
        strategy_id=strategy.id,
        setting_id=setting.id if setting else None,
        symbol=symbol,
        start_time=start_dt,
        end_time=end_dt,
        source_candle_count=0,
        signal_count=0,
        trade_count=0,
        final_equity=0,
        return_pct=0,
        mdd=0,
        win_rate=0,
        pf=0,
        liquidation_count=0,
        config_snapshot=effective_config,
        result_summary={
            "requested_start": start,
            "requested_end": end,
            "max_candles": max_candles,
            "progress_message": "대기 중",
        },
        status="queued",
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def execute_strategy_backtest_job(run_id: int) -> None:
    db = SessionLocal()
    try:
        run = db.query(StrategyBacktestRun).filter(StrategyBacktestRun.id == run_id).first()
        if run is None:
            return

        if run.status not in {"queued", "failed"}:
            return

        run.status = "running"
        run.error_message = None
        run.result_summary = {
            **(run.result_summary or {}),
            "progress_message": "캔들 데이터를 불러오는 중",
        }
        db.commit()

        _execute_strategy_backtest(db, run)
    except Exception as exc:
        db.rollback()
        failed_run = db.query(StrategyBacktestRun).filter(StrategyBacktestRun.id == run_id).first()
        if failed_run is not None:
            failed_run.status = "failed"
            failed_run.error_message = str(exc)[:2000]
            failed_run.result_summary = {
                **(failed_run.result_summary or {}),
                "progress_message": "실패",
            }
            db.commit()
    finally:
        db.close()


def _execute_strategy_backtest(db: Session, run: StrategyBacktestRun) -> None:
    strategy = db.query(TradingStrategy).filter(TradingStrategy.id == run.strategy_id).first()
    if strategy is None:
        raise RuntimeError("전략을 찾을 수 없습니다.")
    if strategy.engine_key != "V57_LONG_SAFE_PUSH":
        raise RuntimeError("현재 백테스트는 V57 엔진 전략만 지원합니다.")

    request_meta = run.result_summary or {}
    max_candles = normalize_max_candles(int(request_meta.get("max_candles") or 120000))
    requested_start = run.start_time
    requested_end = run.end_time

    raw_df = load_backtest_candles(
        db=db,
        user_id=run.user_id,
        symbol=run.symbol,
        start=requested_start,
        end=requested_end,
        max_candles=max_candles,
    )

    if raw_df.empty:
        raise RuntimeError("백테스트할 1분봉 데이터가 없습니다. 먼저 캔들 데이터를 동기화하세요.")

    if len(raw_df) < MIN_BACKTEST_CANDLES:
        raise RuntimeError("백테스트에는 최소 5,000개 이상의 1분봉 데이터가 필요합니다.")

    run.source_candle_count = int(len(raw_df))
    run.result_summary = {
        **request_meta,
        "max_candles": max_candles,
        "progress_message": "지표와 신호를 계산하는 중",
    }
    db.commit()

    # 서버 백테스트는 공식 과거 펀딩비를 포함한다. 조회 실패를 0원으로 숨기지 않는다.
    funding_rates = get_funding_rate_history(
        run.symbol,
        int(pd.Timestamp(raw_df['timestamp'].min()).timestamp() * 1000),
        int(pd.Timestamp(raw_df['timestamp'].max()).timestamp() * 1000),
    )
    diagnostics_buffer = io.StringIO()
    with contextlib.redirect_stdout(diagnostics_buffer):
        with V57ConfigOverride(run.config_snapshot or {}):
            df_1h, df_15m = prepare_data(raw_df)
            signal_events = build_signal_events(df_1h, df_15m)
            equity_df, trades_df = backtest(df_15m, signal_events, funding_rates=funding_rates)
            stats = calculate_stats(trades_df, equity_df)

    if equity_df is None or equity_df.empty:
        calculated_start = raw_df["timestamp"].min().to_pydatetime() if hasattr(raw_df["timestamp"].min(), "to_pydatetime") else raw_df["timestamp"].min()
        calculated_end = raw_df["timestamp"].max().to_pydatetime() if hasattr(raw_df["timestamp"].max(), "to_pydatetime") else raw_df["timestamp"].max()
    else:
        calculated_start = equity_df.index.min().to_pydatetime()
        calculated_end = equity_df.index.max().to_pydatetime()

    result_summary = {
        **request_meta,
        "max_candles": max_candles,
        "progress_message": "완료",
        "engine_revision": "2026-11-24-causal",
        "funding_included": True,
        "funding_payment_count": len(funding_rates),
        "position_limit": 1,
        "execution_model": "15분 OHLC, 동일 봉 손절 우선, 다음 봉 추적 손절 적용",
        "limitations": ["실제 체결 및 청산 엔진의 재현이 아닌 근사치", "봉 내부 가격 순서 미확인"],
        "trades": int(stats.get("trades", 0)),
        "pnl": clean_number(stats.get("pnl")),
        "win_rate": clean_number(stats.get("win_rate")),
        "avg_win": clean_number(stats.get("avg_win")),
        "avg_loss": clean_number(stats.get("avg_loss")),
        "expectancy": clean_number(stats.get("expectancy")),
        "avg_r": clean_number(stats.get("avg_r")),
        "pf": clean_number(stats.get("pf")),
        "pf_infinite": math.isinf(float(stats.get("pf", 0))),
        "mdd": clean_number(stats.get("mdd")),
        "return_pct": clean_number(stats.get("return_pct")),
        "final_equity": clean_number(stats.get("final_equity")),
        "failed_1r": int(stats.get("failed_1r", 0)),
        "max_losing_streak": int(stats.get("max_losing_streak", 0)),
        "worst_trade": clean_number(stats.get("worst_trade")),
        "avg_bars_held": clean_number(stats.get("avg_bars_held")),
        "liquidation_count": int(stats.get("liquidation_count", 0)),
        "signal_count": int(len(signal_events)),
    }

    run.start_time = calculated_start
    run.end_time = calculated_end
    run.source_candle_count = int(len(raw_df))
    run.signal_count = int(len(signal_events))
    run.trade_count = int(stats.get("trades", 0))
    run.final_equity = result_summary["final_equity"]
    run.return_pct = result_summary["return_pct"]
    run.mdd = result_summary["mdd"]
    run.win_rate = result_summary["win_rate"]
    run.pf = result_summary["pf"]
    run.liquidation_count = result_summary["liquidation_count"]
    run.result_summary = result_summary
    run.diagnostics = diagnostics_buffer.getvalue()[-10000:]
    run.status = "success"
    run.error_message = None
    db.commit()
    db.refresh(run)


def run_strategy_backtest(
    db: Session,
    user_id: int,
    symbol: str,
    trading_account_id: int | None,
    start: str | None,
    end: str | None,
    max_candles: int,
) -> StrategyBacktestRun:
    run = create_strategy_backtest_job(
        db=db,
        user_id=user_id,
        symbol=symbol,
        trading_account_id=trading_account_id,
        start=start,
        end=end,
        max_candles=max_candles,
    )
    execute_strategy_backtest_job(run.id)
    db.refresh(run)
    return run


def get_strategy_backtest_run(
    db: Session,
    user_id: int,
    run_id: int,
) -> StrategyBacktestRun:
    run = (
        db.query(StrategyBacktestRun)
        .filter(
            StrategyBacktestRun.id == run_id,
            StrategyBacktestRun.user_id == user_id,
        )
        .first()
    )
    if run is None:
        raise HTTPException(status_code=404, detail="백테스트 작업을 찾을 수 없습니다.")
    return run


def list_strategy_backtest_runs(
    db: Session,
    user_id: int,
    trading_account_id: int | None = None,
    limit: int = 20,
) -> list[StrategyBacktestRun]:
    query = (
        db.query(StrategyBacktestRun)
        .filter(StrategyBacktestRun.user_id == user_id)
        .order_by(StrategyBacktestRun.id.desc())
    )
    if trading_account_id is None:
        query = query.filter(StrategyBacktestRun.trading_account_id.is_(None))
    else:
        query = query.filter(StrategyBacktestRun.trading_account_id == trading_account_id)
    return query.limit(limit).all()
