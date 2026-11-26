from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.jwt import get_current_user
from app.db.session import get_db
from app.models.trading_account import TradingAccount
from app.models.user import User
from app.services.latest_signal_service import (
    get_latest_signal_state_by_account,
    get_latest_signal_state_by_symbol,
    serialize_latest_signal_state,
    upsert_latest_signal_state,
)
from app.services.strategy_signal_service import get_v57_signal_from_db_candles

router = APIRouter(tags=["strategy"])


def _get_active_account_by_symbol(db: Session, user_id: int, symbol: str) -> TradingAccount | None:
    return (
        db.query(TradingAccount)
        .filter(
            TradingAccount.user_id == user_id,
            TradingAccount.symbol == symbol.upper(),
            TradingAccount.is_active == True,
            )
        .order_by(TradingAccount.id.desc())
        .first()
    )


def _calculate_and_save_signal(
    db: Session,
    user_id: int,
    account: TradingAccount,
    sync_recent: bool,
):
    signal_response = get_v57_signal_from_db_candles(
        db=db,
        user_id=user_id,
        symbol=account.symbol,
        sync_recent=sync_recent,
        trading_account_id=account.id,
    )

    state = upsert_latest_signal_state(
        db=db,
        user_id=user_id,
        trading_account_id=account.id,
        symbol=account.symbol,
        signal_response=signal_response,
    )

    return {
        "status": "calculated",
        "message": "신호를 직접 계산하고 DB에 최신 결과로 저장했습니다.",
        "symbol": account.symbol.upper(),
        "strategy": signal_response.get("strategy"),
        "source_interval": signal_response.get("source_interval"),
        "signal": signal_response.get("signal"),
        "latest_signal": serialize_latest_signal_state(state),
    }


@router.get("/strategy/v57/signal/{symbol}")
def get_v57_signal(
    symbol: str,
    live: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    symbol = symbol.upper()
    account = _get_active_account_by_symbol(
        db=db,
        user_id=current_user.id,
        symbol=symbol,
    )

    if account is None:
        raise HTTPException(
            status_code=404,
            detail="해당 심볼의 자동매매 계정을 찾을 수 없습니다. 먼저 자동매매 계정을 생성하세요.",
        )

    if live:
        return _calculate_and_save_signal(
            db=db,
            user_id=current_user.id,
            account=account,
            sync_recent=True,
        )

    state = get_latest_signal_state_by_symbol(
        db=db,
        user_id=current_user.id,
        symbol=symbol,
    )

    if state is None:
        return _calculate_and_save_signal(
            db=db,
            user_id=current_user.id,
            account=account,
            sync_recent=True,
        )

    cached = serialize_latest_signal_state(state)
    signal_response = cached.get("signal_response") if cached else None

    return {
        "status": "cached",
        "message": "DB에 저장된 최신 신호입니다. 실시간 계산이 필요하면 live=true로 호출하세요.",
        "symbol": symbol,
        "strategy": signal_response.get("strategy") if isinstance(signal_response, dict) else None,
        "source_interval": signal_response.get("source_interval") if isinstance(signal_response, dict) else None,
        "signal": signal_response.get("signal") if isinstance(signal_response, dict) else None,
        "latest_signal": cached,
    }


@router.get("/strategy/v57/signal/account/{trading_account_id}")
def get_v57_signal_by_account(
    trading_account_id: int,
    live: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    account = (
        db.query(TradingAccount)
        .filter(
            TradingAccount.id == trading_account_id,
            TradingAccount.user_id == current_user.id,
            TradingAccount.is_active == True,
            )
        .first()
    )

    if account is None:
        raise HTTPException(
            status_code=404,
            detail="자동매매 계정을 찾을 수 없습니다.",
        )

    if live:
        result = _calculate_and_save_signal(
            db=db,
            user_id=current_user.id,
            account=account,
            sync_recent=True,
        )
        result["trading_account_id"] = trading_account_id
        return result

    state = get_latest_signal_state_by_account(
        db=db,
        user_id=current_user.id,
        trading_account_id=trading_account_id,
    )

    if state is None:
        result = _calculate_and_save_signal(
            db=db,
            user_id=current_user.id,
            account=account,
            sync_recent=True,
        )
        result["trading_account_id"] = trading_account_id
        return result

    cached = serialize_latest_signal_state(state)
    signal_response = cached.get("signal_response") if cached else None

    return {
        "status": "cached",
        "message": "DB에 저장된 최신 신호입니다. 실시간 계산이 필요하면 live=true로 호출하세요.",
        "trading_account_id": trading_account_id,
        "symbol": account.symbol.upper(),
        "strategy": signal_response.get("strategy") if isinstance(signal_response, dict) else None,
        "source_interval": signal_response.get("source_interval") if isinstance(signal_response, dict) else None,
        "signal": signal_response.get("signal") if isinstance(signal_response, dict) else None,
        "latest_signal": cached,
    }
