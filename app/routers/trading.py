from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import CANDLE_WARMUP_DAYS, DRY_RUN
from app.core.jwt import get_current_user
from app.db.session import get_db
from app.models.error_log import ErrorLog
from app.models.trade_log import TradeLog
from app.models.trading_account import TradingAccount
from app.models.user import User
from app.schemas import BotTickRequest, TradingAccountCreateRequest, TradingAccountResponse
from app.scheduler.bot_scheduler import start_account_job, stop_account_job
from app.services.account_service import get_api_key_owned_by_user, get_trading_account_owned_by_user
from app.services.binance_candle_sync import BinanceCandleSyncService
from app.services.binance_service import get_decrypted_api_keys
from app.services.bot_service import execute_bot_tick, reconcile_position_and_open_orders

router = APIRouter(tags=["trading"])


def ensure_active_trading_account(account: TradingAccount):
    if not account.is_active:
        raise HTTPException(
            status_code=400,
            detail="비활성화된 자동매매 계정입니다.",
        )


@router.post("/trading/accounts", response_model=TradingAccountResponse)
def create_trading_account(
    body: TradingAccountCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    get_api_key_owned_by_user(db, current_user.id, body.api_key_id)

    account = TradingAccount(
        user_id=current_user.id,
        api_key_id=body.api_key_id,
        symbol=body.symbol.upper(),
        leverage=body.leverage,
        max_risk_ratio=body.max_risk_ratio,
        max_daily_loss_ratio=body.max_daily_loss_ratio,
        is_running=False,
        is_active=True,
    )

    db.add(account)
    db.commit()
    db.refresh(account)

    return account


@router.get("/trading/accounts", response_model=List[TradingAccountResponse])
def list_trading_accounts(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return (
        db.query(TradingAccount)
        .filter(
            TradingAccount.user_id == current_user.id,
            TradingAccount.is_active == True,
            )
        .order_by(TradingAccount.id.desc())
        .all()
    )


@router.get("/trading/status")
def trading_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    accounts = (
        db.query(TradingAccount)
        .filter(
            TradingAccount.user_id == current_user.id,
            TradingAccount.is_active == True,
            )
        .order_by(TradingAccount.id.desc())
        .all()
    )

    total_accounts = len(accounts)
    running_accounts = len([account for account in accounts if account.is_running])

    latest_log = (
        db.query(TradeLog)
        .filter(TradeLog.user_id == current_user.id)
        .order_by(TradeLog.id.desc())
        .first()
    )

    total_order_count = (
        db.query(func.count(TradeLog.id))
        .filter(TradeLog.user_id == current_user.id)
        .scalar()
    )

    total_pnl = (
        db.query(func.coalesce(func.sum(TradeLog.pnl), 0))
        .filter(TradeLog.user_id == current_user.id)
        .scalar()
    )

    latest_logs = (
        db.query(TradeLog)
        .filter(TradeLog.user_id == current_user.id)
        .order_by(TradeLog.id.desc())
        .limit(5)
        .all()
    )

    account_items = []

    for account in accounts:
        account_latest_log = (
            db.query(TradeLog)
            .filter(
                TradeLog.user_id == current_user.id,
                TradeLog.trading_account_id == account.id,
                )
            .order_by(TradeLog.id.desc())
            .first()
        )

        account_items.append(
            {
                "id": account.id,
                "api_key_id": account.api_key_id,
                "market_type": account.market_type,
                "symbol": account.symbol,
                "leverage": account.leverage,
                "max_risk_ratio": account.max_risk_ratio,
                "max_daily_loss_ratio": account.max_daily_loss_ratio,
                "is_running": account.is_running,
                "is_active": account.is_active,
                "created_at": account.created_at,
                "latest_log": serialize_trade_log(account_latest_log),
            }
        )

    return {
        "status": "ok",
        "dry_run": DRY_RUN,
        "is_live_trading": not DRY_RUN,
        "user_id": current_user.id,
        "summary": {
            "total_accounts": total_accounts,
            "running_accounts": running_accounts,
            "stopped_accounts": total_accounts - running_accounts,
            "total_logs": total_order_count,
            "total_pnl": total_pnl,
            "latest_symbol": latest_log.symbol if latest_log else None,
            "latest_side": latest_log.side if latest_log else None,
            "latest_status": latest_log.status if latest_log else None,
            "latest_strategy": latest_log.strategy if latest_log else None,
            "latest_created_at": latest_log.created_at if latest_log else None,
        },
        "accounts": account_items,
        "latest_logs": [serialize_trade_log(log) for log in latest_logs],
    }


@router.post("/trading/accounts/{trading_account_id}/start")
def start_trading_account(
    trading_account_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    account = get_trading_account_owned_by_user(db, current_user.id, trading_account_id)
    ensure_active_trading_account(account)

    sync_result = BinanceCandleSyncService.warmup_historical_1m_candles(
        db=db,
        user_id=current_user.id,
        symbol=account.symbol,
        days=CANDLE_WARMUP_DAYS,
    )

    account.is_running = True
    db.commit()

    start_account_job(account.id)

    return {
        "status": "started",
        "trading_account_id": account.id,
        "symbol": account.symbol,
        "warmup_days": CANDLE_WARMUP_DAYS,
        "sync_result": sync_result,
    }


@router.post("/trading/accounts/{trading_account_id}/stop")
def stop_trading_account(
    trading_account_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    account = get_trading_account_owned_by_user(db, current_user.id, trading_account_id)
    ensure_active_trading_account(account)

    account.is_running = False
    db.commit()

    stop_account_job(account.id)

    return {
        "status": "stopped",
        "trading_account_id": account.id,
        "symbol": account.symbol,
    }


@router.post("/bot/tick")
def bot_tick(
    body: BotTickRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    account = get_trading_account_owned_by_user(db, current_user.id, body.trading_account_id)
    ensure_active_trading_account(account)

    return execute_bot_tick(db=db, account=account)


@router.post("/trading/accounts/{trading_account_id}/reconcile")
def reconcile_trading_account(
    trading_account_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    account = get_trading_account_owned_by_user(db, current_user.id, trading_account_id)
    ensure_active_trading_account(account)

    api_key_item = get_api_key_owned_by_user(db, current_user.id, account.api_key_id)
    api_key, secret_key = get_decrypted_api_keys(api_key_item)

    return reconcile_position_and_open_orders(
        api_key=api_key,
        secret_key=secret_key,
        symbol=account.symbol,
    )


@router.get("/trading/logs")
def trading_logs(
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    logs = (
        db.query(TradeLog)
        .filter(TradeLog.user_id == current_user.id)
        .order_by(TradeLog.id.desc())
        .limit(limit)
        .all()
    )

    return [serialize_trade_log(log) for log in logs]


@router.get("/trading/error-logs")
def error_logs(
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    logs = (
        db.query(ErrorLog)
        .filter((ErrorLog.user_id == current_user.id) | (ErrorLog.user_id == None))
        .order_by(ErrorLog.id.desc())
        .limit(limit)
        .all()
    )

    return [
        {
            "id": log.id,
            "user_id": log.user_id,
            "trading_account_id": log.trading_account_id,
            "level": log.level,
            "source": log.source,
            "message": log.message,
            "raw_data": log.raw_data,
            "created_at": log.created_at,
        }
        for log in logs
    ]


@router.post("/trading/emergency-stop")
def emergency_stop_all_accounts(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    accounts = (
        db.query(TradingAccount)
        .filter(
            TradingAccount.user_id == current_user.id,
            TradingAccount.is_active == True,
            )
        .all()
    )

    stopped_account_ids = []

    for account in accounts:
        if account.is_running:
            account.is_running = False
            stop_account_job(account.id)
            stopped_account_ids.append(account.id)

    db.commit()

    return {
        "status": "emergency_stopped",
        "message": "모든 자동매매 계정을 긴급 정지했습니다.",
        "stopped_count": len(stopped_account_ids),
        "stopped_account_ids": stopped_account_ids,
    }


@router.delete("/trading/accounts/{trading_account_id}")
def delete_trading_account(
    trading_account_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    account = get_trading_account_owned_by_user(
        db,
        current_user.id,
        trading_account_id,
    )

    ensure_active_trading_account(account)

    if account.is_running:
        raise HTTPException(
            status_code=400,
            detail="실행 중인 자동매매 계정은 비활성화할 수 없습니다. 먼저 정지하세요.",
        )

    account.is_active = False
    db.commit()

    return {
        "status": "success",
        "message": "자동매매 계정이 비활성화되었습니다.",
        "trading_account_id": account.id,
    }


def serialize_trade_log(log: TradeLog | None):
    if log is None:
        return None

    return {
        "id": log.id,
        "user_id": log.user_id,
        "api_key_id": log.api_key_id,
        "trading_account_id": log.trading_account_id,
        "symbol": log.symbol,
        "side": log.side,
        "order_type": log.order_type,
        "qty": log.qty,
        "executed_qty": log.executed_qty,
        "price": log.price,
        "avg_fill_price": log.avg_fill_price,
        "reduce_only": log.reduce_only,
        "dry_run": log.dry_run,
        "status": log.status,
        "pnl": log.pnl,
        "fee": log.fee,
        "strategy": log.strategy,
        "signal_time": log.signal_time,
        "stop_price": log.stop_price,
        "take_profit_price": log.take_profit_price,
        "entry_order_id": log.entry_order_id,
        "stop_order_id": log.stop_order_id,
        "take_profit_order_id": log.take_profit_order_id,
        "client_order_id": log.client_order_id,
        "emergency_close_executed": log.emergency_close_executed,
        "emergency_close_order_id": log.emergency_close_order_id,
        "error_message": log.error_message,
        "raw_response": log.raw_response,
        "created_at": log.created_at,
    }