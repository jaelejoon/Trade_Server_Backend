from datetime import datetime, timezone
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.core.config import CANDLE_WARMUP_DAYS, ENABLE_SPOT_TESTNET_ORDERS, ENABLE_TRADING_SCHEDULER
from app.core.jwt import get_current_user
from app.core.trading_limits import normalize_trading_symbol
from app.db.session import get_db
from app.models.bot_signal_state import BotSignalState
from app.models.error_log import ErrorLog
from app.models.spot_fill import SpotFill
from app.models.trade_log import TradeLog
from app.models.trading_account import TradingAccount
from app.models.user import User
from app.routers.admin import require_admin
from app.schemas import BotTickRequest, TradingAccountCreateRequest, TradingAccountResponse
from app.scheduler.bot_scheduler import start_account_job, stop_account_job
from app.services.account_service import get_active_trading_account_owned_by_user, get_api_key_owned_by_user, get_trading_account_owned_by_user
from app.services.binance_candle_sync import BinanceCandleSyncService
from app.core.security import sanitize_sensitive_text
from app.services.binance_service import get_decrypted_api_keys
from app.services.bot_service import execute_bot_tick

from app.services.spot_service import SpotExchange
from app.services.spot_v12_runtime import strategy_status
from app.services.spot_v12_controls import OperationsRequest, change_operations, controls, get_operations
from app.services.spot_recovery_service import inspect_recovery
from app.services.error_log_service import create_error_log

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
        max_risk_ratio=body.max_risk_ratio,
        max_daily_loss_ratio=body.max_daily_loss_ratio,
        dry_run=current_user.dry_run,
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


def _spot_journal(db, account_id):
    state = db.query(BotSignalState).filter_by(trading_account_id=account_id).one_or_none()
    journal = (state.runtime_state or {}).get("spot") if state else None
    return journal if isinstance(journal, dict) else {}


def _operation_runtime(db, account_id):
    state = db.query(BotSignalState).filter_by(trading_account_id=account_id).one_or_none()
    return state.runtime_state or {} if state else {}


def _stored_stop(db, account):
    if account.is_running:
        return None, None
    config = controls(_operation_runtime(db, account.id))
    if config.get("stop_reason"):
        return config["stop_reason"], config.get("stopped_at")
    clauses = [and_(ErrorLog.source == "spot_recovery",
                    ErrorLog.message == "봇 체결 기록 확인 필요")]
    if _spot_journal(db, account.id).get("halted") is True:
        clauses.append(and_(ErrorLog.source == "spot_lifecycle",
                            ErrorLog.message != "거래소 잔고와 봇 보유량 불일치"))
    log = (db.query(ErrorLog)
           .filter(ErrorLog.trading_account_id == account.id, ErrorLog.user_id == account.user_id,
                   or_(*clauses))
           .order_by(ErrorLog.id.desc()).first())
    if log is None:
        return None, None
    stopped_at = log.created_at
    if stopped_at is not None and stopped_at.tzinfo is None:
        stopped_at = stopped_at.replace(tzinfo=timezone.utc)
    return log.message, stopped_at


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

        stop_reason, stopped_at = _stored_stop(db, account)
        account_items.append(
            {
                "id": account.id,
                "api_key_id": account.api_key_id,
                "market_type": account.market_type,
                "symbol": account.symbol,
                "max_risk_ratio": account.max_risk_ratio,
                "max_daily_loss_ratio": account.max_daily_loss_ratio,
                "dry_run": account.dry_run,
                "is_running": account.is_running,
                "stop_reason": stop_reason,
                "stopped_at": stopped_at,
                "is_active": account.is_active,
                "created_at": account.created_at,
                "latest_log": serialize_trade_log(account_latest_log),
            }
        )

    return {
        "status": "ok",
        "scheduler_enabled": ENABLE_TRADING_SCHEDULER,
        "dry_run": all(account.dry_run for account in accounts) if accounts else current_user.dry_run,
        "is_live_trading": False,
        "spot_testnet_orders_enabled": ENABLE_SPOT_TESTNET_ORDERS,
        "enable_live_trading": current_user.enable_live_trading,
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
    if not ENABLE_TRADING_SCHEDULER:
        raise HTTPException(status_code=409, detail="자동매매 스케줄러가 꺼져 있습니다.")
    account = get_active_trading_account_owned_by_user(db, current_user.id, trading_account_id)

    try:
        symbol = normalize_trading_symbol(account.symbol)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if account.market_type != "SPOT":
        raise HTTPException(status_code=409, detail="현물 계정을 새로 등록하세요.")
    settings = controls(_operation_runtime(db, account.id))
    if not settings["enabled"] or settings.get("stop_reason"):
        raise HTTPException(409, "관리자 운용 설정이나 중지 사유를 확인하세요.")
    sync_result = BinanceCandleSyncService.warmup_historical_1m_candles(
        db=db,
        user_id=current_user.id,
        symbol=symbol,
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
    account = get_active_trading_account_owned_by_user(db, current_user.id, trading_account_id)

    account.is_running = False
    db.commit()

    stop_account_job(account.id)

    return {
        "status": "stopped",
        "trading_account_id": account.id,
        "symbol": account.symbol,
    }


@router.post("/trading/accounts/{account_id}/spot-testnet-check", include_in_schema=False)
def run_spot_testnet_check(
    account_id: int,
    action: Literal["entry", "reject_oco", "advance"],
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    if not ENABLE_SPOT_TESTNET_ORDERS:
        raise HTTPException(status_code=404, detail="현물 테스트넷 주문 검증이 꺼져 있습니다.")
    account = get_active_trading_account_owned_by_user(db, current_user.id, account_id)
    result = execute_bot_tick(db, account, test_entry=None if action == "advance" else action)
    if ENABLE_TRADING_SCHEDULER and action == "entry" and result["status"] == "protected":
        start_account_job(account_id)
    return result


@router.post("/bot/tick")
def bot_tick(
    body: BotTickRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    account = get_active_trading_account_owned_by_user(db, current_user.id, body.trading_account_id)

    return execute_bot_tick(db=db, account=account)


@router.post("/trading/accounts/{trading_account_id}/reconcile")
def reconcile_trading_account(
    trading_account_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    account = get_active_trading_account_owned_by_user(db, current_user.id, trading_account_id)

    api_key_item = get_api_key_owned_by_user(db, current_user.id, account.api_key_id)
    api_key, secret_key = get_decrypted_api_keys(api_key_item)

    exchange = SpotExchange(api_key, secret_key)
    recovery, orders = inspect_recovery(db, account.id, exchange)
    if recovery["halt"]:
        account.is_running = False
        stop_account_job(account.id)
    if recovery["alert"]:
        create_error_log(
            db, "spot_recovery", recovery["alert"], user_id=account.user_id,
            trading_account_id=account.id, level="WARNING",
        )
    return {"recovery": recovery, "open_order_lists": orders}


@router.get('/trading/accounts/{trading_account_id}/portfolio')
def get_trading_account_portfolio(
    trading_account_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    account = get_active_trading_account_owned_by_user(db, current_user.id, trading_account_id)
    api_key_item = get_api_key_owned_by_user(db, current_user.id, account.api_key_id)
    api_key, secret_key = get_decrypted_api_keys(api_key_item)

    balances = SpotExchange(api_key, secret_key).account()["balances"]
    return {"status": "ok", "symbol": account.symbol, "balances": balances}


@router.get("/trading/accounts/{trading_account_id}/fills")
def get_spot_fills(
    trading_account_id: int,
    limit: int = Query(20, ge=1, le=100),
    before_trade_id: int | None = Query(None, gt=0, le=9223372036854775807),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    account = get_trading_account_owned_by_user(db, current_user.id, trading_account_id)
    query = db.query(SpotFill).filter(SpotFill.trading_account_id == account.id)
    if before_trade_id is not None:
        query = query.filter(SpotFill.trade_id < before_trade_id)
    rows = query.order_by(SpotFill.trade_id.desc()).limit(limit + 1).all()
    items = [
        {"trade_id": str(row.trade_id), "order_id": str(row.order_id), "side": row.side,
         "quantity": format(row.quantity, "f"), "commission": format(row.commission, "f"),
         "commission_asset": row.commission_asset}
        for row in rows[:limit]
    ]
    return {"trading_account_id": account.id, "symbol": account.symbol, "items": items,
            "next_before_trade_id": items[-1]["trade_id"] if len(rows) > limit else None}


@router.get("/trading/accounts/{trading_account_id}/oco")
def get_bot_oco(
    trading_account_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    account = get_trading_account_owned_by_user(db, current_user.id, trading_account_id)
    journal = _spot_journal(db, account.id)
    client_id = journal.get("oco")
    if not isinstance(client_id, str) or not client_id:
        return {"trading_account_id": account.id, "oco": None}

    item = get_api_key_owned_by_user(db, current_user.id, account.api_key_id)
    exchange = SpotExchange(*get_decrypted_api_keys(item))
    oco = {"source": "bot_journal", "client_order_list_id": client_id,
           "order_list_id": None, "stop_price": journal.get("stop"),
           "take_profit_price": journal.get("target"), "quantity": None, "status": "UNKNOWN"}
    order_list = exchange.lookup(client_id, order_list=True)
    if (order_list and order_list.get("symbol") == account.symbol
            and order_list.get("listClientOrderId") == client_id
            and type(order_list.get("orderListId")) is int):
        list_id = order_list["orderListId"]
        oco["order_list_id"] = str(list_id)
        oco["status"] = order_list.get("listOrderStatus") or "UNKNOWN"
        legs = order_list.get("orders") or []
        if len(legs) == 2:
            orders = [exchange.lookup(leg["clientOrderId"]) for leg in legs]
            if all(order and order.get("orderListId") == list_id
                   and order.get("symbol") == account.symbol and order.get("side") == "SELL"
                   for order in orders):
                by_type = {order["type"]: order for order in orders}
                stop = by_type.get("STOP_LOSS")
                target = by_type.get("LIMIT_MAKER")
                if stop and target:
                    oco["stop_price"] = stop.get("stopPrice") or oco["stop_price"]
                    oco["take_profit_price"] = target.get("price") or oco["take_profit_price"]
                    if stop.get("origQty") == target.get("origQty"):
                        oco["quantity"] = stop.get("origQty")
    return {"trading_account_id": account.id, "oco": oco}


@router.get("/trading/accounts/{trading_account_id}/strategy")
def get_spot_strategy(
    trading_account_id: int,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    result = strategy_status(db, trading_account_id, datetime.now(timezone.utc))
    if result is None:
        raise HTTPException(status_code=404, detail="현물 계정을 찾을 수 없습니다.")
    return result


@router.get("/trading/accounts/{trading_account_id}/operations")
def read_v12_operations(trading_account_id: int, db: Session = Depends(get_db),
                        _admin: User = Depends(require_admin)):
    _, state = get_operations(db, trading_account_id)
    return controls((state.runtime_state or {}) if state else {})


@router.patch("/trading/accounts/{trading_account_id}/operations")
def update_v12_operations(trading_account_id: int, body: OperationsRequest,
                          db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    account, settings = change_operations(db, trading_account_id, admin.id, body=body)
    (start_account_job if account.is_running else stop_account_job)(account.id)
    return settings


@router.post("/trading/accounts/{trading_account_id}/operations/resume")
def resume_v12_operations(trading_account_id: int, db: Session = Depends(get_db),
                          admin: User = Depends(require_admin)):
    account, settings = change_operations(db, trading_account_id, admin.id, resume=True)
    if account.is_running:
        start_account_job(account.id)
    return settings


@router.get("/trading/logs")
def trading_logs(
    limit: int = 100,
    trading_account_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = db.query(TradeLog).filter(TradeLog.user_id == current_user.id)

    if trading_account_id is not None:
        account = get_trading_account_owned_by_user(
            db=db,
            user_id=current_user.id,
            trading_account_id=trading_account_id,
        )
        query = query.filter(TradeLog.trading_account_id == account.id)

    logs = (
        query
        .order_by(TradeLog.id.desc())
        .limit(limit)
        .all()
    )

    return [serialize_trade_log(log) for log in logs]


@router.get("/trading/error-logs")
def error_logs(
    limit: int = 100,
    trading_account_id: Optional[int] = None,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    query = db.query(ErrorLog)
    if trading_account_id is not None:
        query = query.filter(ErrorLog.trading_account_id == trading_account_id)

    logs = (
        query
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
            "message": "자동매매 처리 중 오류가 발생했습니다.",
            "raw_data": None,
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
    account = get_active_trading_account_owned_by_user(
        db,
        current_user.id,
        trading_account_id,
    )

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
        "error_message": "자동매매 처리 중 오류가 발생했습니다." if log.error_message else None,
        "raw_response": sanitize_sensitive_text(log.raw_response) if log.raw_response and not log.error_message else None,
        "created_at": log.created_at,
    }