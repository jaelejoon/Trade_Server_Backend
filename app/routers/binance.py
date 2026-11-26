from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.config import BINANCE_TESTNET
from app.core.jwt import get_current_user
from app.db.session import get_db
from app.models.binance_api_key import BinanceApiKey
from app.models.user import User
from app.schemas import ApiKeyRegisterRequest, ApiKeyResponse
from app.services.account_service import get_api_key_owned_by_user
from app.services.api_key_security_service import encrypt_api_credentials
from app.services.binance_service import check_binance_permissions, get_decrypted_api_keys
from app.services.spot_service import SpotExchange
from app.models.trading_account import TradingAccount

router = APIRouter(tags=["binance"])


@router.post("/binance/api-keys", response_model=ApiKeyResponse)
def register_binance_api_key(
    body: ApiKeyRegisterRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    api_key_encrypted, secret_key_encrypted = encrypt_api_credentials(
        body.api_key,
        body.secret_key,
    )

    if BINANCE_TESTNET:
        item = BinanceApiKey(
            user_id=current_user.id,
            api_key_encrypted=api_key_encrypted,
            secret_key_encrypted=secret_key_encrypted,
            label=body.label,
            is_active=True,
            enable_reading=True,
            enable_withdrawals=False,
            ip_restricted=False,
        )
    else:
        permissions = check_binance_permissions(api_key=body.api_key, secret_key=body.secret_key)
        if bool(permissions.get("enableWithdrawals", False)):
            raise HTTPException(status_code=400, detail="출금 권한이 켜진 API Key는 등록할 수 없습니다.")

        item = BinanceApiKey(
            user_id=current_user.id,
            api_key_encrypted=api_key_encrypted,
            secret_key_encrypted=secret_key_encrypted,
            label=body.label,
            is_active=True,
            enable_reading=bool(permissions.get("enableReading", False)),
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


@router.get("/binance/spot/balance/{api_key_id}")
def spot_balance(
    api_key_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    item = get_api_key_owned_by_user(db, current_user.id, api_key_id)
    return SpotExchange(*get_decrypted_api_keys(item)).account()["balances"]
