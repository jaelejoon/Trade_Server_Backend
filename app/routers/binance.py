from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.config import BINANCE_TESTNET, DRY_RUN, ENABLE_LIVE_TRADING
from app.core.jwt import get_current_user
from app.core.security import encrypt_text
from app.db.session import get_db
from app.models.binance_api_key import BinanceApiKey
from app.models.user import User
from app.schemas import ApiKeyRegisterRequest, ApiKeyResponse, FuturesMarketOrderRequest, SetLeverageRequest
from app.services.account_service import get_api_key_owned_by_user
from app.services.binance_service import (
    check_binance_permissions,
    futures_market_order_raw,
    futures_set_leverage_raw,
    get_decrypted_api_keys,
    get_futures_balance_raw,
    get_futures_positions_raw,
    get_futures_price,
)
from app.services.trade_log_service import create_trade_log

from app.models.trading_account import TradingAccount

router = APIRouter(tags=["binance"])


@router.post("/binance/api-keys", response_model=ApiKeyResponse)
def register_binance_api_key(
    body: ApiKeyRegisterRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if BINANCE_TESTNET:
        item = BinanceApiKey(
            user_id=current_user.id,
            api_key_encrypted=encrypt_text(body.api_key),
            secret_key_encrypted=encrypt_text(body.secret_key),
            label=body.label,
            is_active=True,
            enable_reading=True,
            enable_futures=True,
            enable_withdrawals=False,
            ip_restricted=False,
        )
    else:
        permissions = check_binance_permissions(api_key=body.api_key, secret_key=body.secret_key)
        if bool(permissions.get("enableWithdrawals", False)):
            raise HTTPException(status_code=400, detail="출금 권한이 켜진 API Key는 등록할 수 없습니다.")

        item = BinanceApiKey(
            user_id=current_user.id,
            api_key_encrypted=encrypt_text(body.api_key),
            secret_key_encrypted=encrypt_text(body.secret_key),
            label=body.label,
            is_active=True,
            enable_reading=bool(permissions.get("enableReading", False)),
            enable_futures=bool(permissions.get("enableFutures", False)),
            enable_withdrawals=bool(permissions.get("enableWithdrawals", False)),
            ip_restricted=bool(permissions.get("ipRestrict", False)),
        )

    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@router.get("/binance/api-keys", response_model=List[ApiKeyResponse])
def list_binance_api_keys(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return (
        db.query(BinanceApiKey)
        .filter(BinanceApiKey.user_id == current_user.id)
        .order_by(BinanceApiKey.id.desc())
        .all()
    )


@router.delete("/binance/api-keys/{api_key_id}")
def delete_binance_api_key(
    api_key_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    item = get_api_key_owned_by_user(
        db,
        current_user.id,
        api_key_id,
    )

    running_accounts = (
        db.query(TradingAccount)
        .filter(
            TradingAccount.user_id == current_user.id,
            TradingAccount.api_key_id == api_key_id,
            TradingAccount.is_running == True,
            )
        .count()
    )

    if running_accounts > 0:
        raise HTTPException(
            status_code=400,
            detail="실행 중인 자동매매 계정이 있어 API Key를 비활성화할 수 없습니다.",
        )

    item.is_active = False

    db.commit()

    return {
        "status": "success",
        "message": "API Key가 비활성화되었습니다.",
    }


@router.get("/binance/futures/balance/{api_key_id}")
def futures_balance(
    api_key_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    item = get_api_key_owned_by_user(db, current_user.id, api_key_id)
    api_key, secret_key = get_decrypted_api_keys(item)
    return get_futures_balance_raw(api_key, secret_key)


@router.get("/binance/futures/positions/{api_key_id}")
def futures_positions(
    api_key_id: int,
    symbol: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    item = get_api_key_owned_by_user(db, current_user.id, api_key_id)
    api_key, secret_key = get_decrypted_api_keys(item)
    return get_futures_positions_raw(api_key, secret_key, symbol=symbol)


@router.get("/binance/futures/price/{symbol}")
def futures_price(symbol: str):
    return {"symbol": symbol.upper(), "price": get_futures_price(symbol)}


@router.post("/binance/futures/set-leverage")
def futures_set_leverage(
    body: SetLeverageRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    item = get_api_key_owned_by_user(db, current_user.id, body.api_key_id)
    api_key, secret_key = get_decrypted_api_keys(item)
    result = futures_set_leverage_raw(api_key, secret_key, body.symbol, body.leverage)
    return {"status": "success", "result": result}


@router.post("/binance/futures/order/market")
def futures_market_order(
    body: FuturesMarketOrderRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    item = get_api_key_owned_by_user(db, current_user.id, body.api_key_id)
    api_key, secret_key = get_decrypted_api_keys(item)
    price = get_futures_price(body.symbol)

    if DRY_RUN or not ENABLE_LIVE_TRADING:
        result = {
            "dry_run": True,
            "symbol": body.symbol.upper(),
            "side": body.side,
            "quantity": body.quantity,
            "reduce_only": body.reduce_only,
        }
    else:
        result = futures_market_order_raw(
            api_key=api_key,
            secret_key=secret_key,
            symbol=body.symbol,
            side=body.side,
            quantity=body.quantity,
            reduce_only=body.reduce_only,
        )

    create_trade_log(
        db=db,
        user_id=current_user.id,
        api_key_id=body.api_key_id,
        trading_account_id=None,
        symbol=body.symbol,
        side=body.side,
        order_type="MARKET",
        qty=body.quantity,
        price=price,
        reduce_only=body.reduce_only,
        dry_run=DRY_RUN,
        status_value="DRY_RUN" if DRY_RUN else "SUBMITTED",
        strategy=None,
        signal_time=None,
        stop_price=None,
        take_profit_price=None,
        raw_response=str(result),
    )

    return {"status": "success", "dry_run": DRY_RUN, "result": result}
