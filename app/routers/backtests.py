from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy.orm import Session

from app.core.jwt import get_current_user
from app.db.session import get_db
from app.models.user import User
from app.schemas import StrategyBacktestRunRequest
from app.services.strategy_backtest_service import (
    create_strategy_backtest_job,
    execute_strategy_backtest_job,
    get_strategy_backtest_run,
    list_strategy_backtest_runs,
    serialize_backtest_run,
)

router = APIRouter(prefix="/backtests", tags=["backtests"])


@router.post("/run")
def run_my_backtest(
    body: StrategyBacktestRunRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    run = create_strategy_backtest_job(
        db=db,
        user_id=current_user.id,
        symbol=body.symbol,
        trading_account_id=body.trading_account_id,
        start=body.start,
        end=body.end,
        max_candles=body.max_candles,
    )
    background_tasks.add_task(execute_strategy_backtest_job, run.id)
    return {"status": "queued", "run": serialize_backtest_run(run)}


@router.get("/{run_id}")
def get_my_backtest_run(
    run_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    run = get_strategy_backtest_run(db=db, user_id=current_user.id, run_id=run_id)
    return serialize_backtest_run(run)


@router.get("")
def get_my_backtest_runs(
    trading_account_id: int | None = None,
    limit: int = 20,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if limit < 1:
        limit = 1
    if limit > 100:
        limit = 100

    runs = list_strategy_backtest_runs(
        db=db,
        user_id=current_user.id,
        trading_account_id=trading_account_id,
        limit=limit,
    )
    return [serialize_backtest_run(run) for run in runs]
