from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.binance_api_key import BinanceApiKey
from app.models.bot_signal_state import BotSignalState
from app.models.trading_account import TradingAccount


def get_api_key_owned_by_user(db: Session, user_id: int, api_key_id: int) -> BinanceApiKey:
    item = (
        db.query(BinanceApiKey)
        .filter(
            BinanceApiKey.id == api_key_id,
            BinanceApiKey.user_id == user_id,
            BinanceApiKey.is_active == True,
        )
        .first()
    )

    if not item:
        raise HTTPException(status_code=404, detail="API Key를 찾을 수 없습니다.")

    return item


def get_trading_account_owned_by_user(db: Session, user_id: int, trading_account_id: int) -> TradingAccount:
    account = (
        db.query(TradingAccount)
        .filter(
            TradingAccount.id == trading_account_id,
            TradingAccount.user_id == user_id,
        )
        .first()
    )

    if not account:
        raise HTTPException(status_code=404, detail="자동매매 계정 설정을 찾을 수 없습니다.")

    return account


def get_or_create_bot_state(db: Session, trading_account_id: int) -> BotSignalState:
    state = (
        db.query(BotSignalState)
        .filter(BotSignalState.trading_account_id == trading_account_id)
        .first()
    )

    if state:
        return state

    state = BotSignalState(
        trading_account_id=trading_account_id,
        last_signal_time=None,
        last_strategy=None,
    )

    db.add(state)
    db.commit()
    db.refresh(state)
    return state
