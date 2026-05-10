from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class ApiKeyRegisterRequest(BaseModel):
    api_key: str = Field(min_length=10)
    secret_key: str = Field(min_length=10)
    label: str = "default"


class ApiKeyResponse(BaseModel):
    id: int
    label: str
    is_active: bool
    enable_reading: bool
    enable_futures: bool
    enable_withdrawals: bool
    ip_restricted: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class TradingAccountCreateRequest(BaseModel):
    api_key_id: int
    symbol: str = "BTCUSDT"
    leverage: int = Field(default=2, ge=1, le=10)
    max_risk_ratio: float = Field(default=0.03, gt=0, le=0.2)
    max_daily_loss_ratio: float = Field(default=0.03, gt=0, le=0.2)


class TradingAccountResponse(BaseModel):
    id: int
    api_key_id: int
    market_type: str
    symbol: str
    leverage: int
    max_risk_ratio: float
    max_daily_loss_ratio: float
    is_running: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class FuturesMarketOrderRequest(BaseModel):
    api_key_id: int
    symbol: str = "BTCUSDT"
    side: Literal["BUY", "SELL"]
    quantity: float = Field(gt=0)
    reduce_only: bool = False


class SetLeverageRequest(BaseModel):
    api_key_id: int
    symbol: str = "BTCUSDT"
    leverage: int = Field(ge=1, le=10)


class BotTickRequest(BaseModel):
    trading_account_id: int
