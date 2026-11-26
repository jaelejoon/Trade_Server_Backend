from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, EmailStr, Field

from app.core.trading_limits import TRADING_SYMBOL, normalize_trading_symbol


TradingSymbol = Annotated[str, AfterValidator(normalize_trading_symbol)]


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    dry_run: bool = True


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserResponse(BaseModel):
    id: int
    email: EmailStr
    dry_run: bool
    is_admin: bool
    is_prime_admin: bool
    is_active: bool
    enable_live_trading: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class ApiKeyRegisterRequest(BaseModel):
    api_key: str = Field(min_length=10)
    secret_key: str = Field(min_length=10)
    label: str = "default"


class ApiKeyResponse(BaseModel):
    id: int
    label: str
    masked_api_key: str
    is_active: bool
    enable_reading: bool
    enable_withdrawals: bool
    ip_restricted: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class TradingAccountCreateRequest(BaseModel):
    api_key_id: int
    symbol: TradingSymbol = TRADING_SYMBOL
    max_risk_ratio: float = Field(default=0.03, gt=0, le=0.2)
    max_daily_loss_ratio: float = Field(default=0.03, gt=0, le=0.2)


class TradingAccountResponse(BaseModel):
    id: int
    api_key_id: int
    market_type: str
    symbol: str
    max_risk_ratio: float
    max_daily_loss_ratio: float
    dry_run: bool
    is_running: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class BotTickRequest(BaseModel):
    trading_account_id: int


class AdminUserAdminUpdateRequest(BaseModel):
    is_admin: bool
    is_prime_admin: bool


class AdminUserActiveUpdateRequest(BaseModel):
    is_active: bool


class AdminUserLiveTradingUpdateRequest(BaseModel):
    enable_live_trading: bool


class AdminPrimeAdminDelegateRequest(BaseModel):
    target_user_id: int

class StrategyBacktestRunRequest(BaseModel):
    symbol: str = Field(default="BTCUSDT", min_length=1)
    trading_account_id: int | None = None
    start: str | None = None
    end: str | None = None
    max_candles: int = Field(default=120000, ge=1, le=3000000)



class StrategyCreateRequest(BaseModel):
    code: str = Field(min_length=1)
    name: str = Field(min_length=1)
    version: str = Field(default="1", min_length=1)
    description: str = ""
    default_config: dict = Field(default_factory=dict)
    editable_fields: list[str] = Field(default_factory=list)


class StrategySelectRequest(BaseModel):
    strategy_id: int = Field(gt=0)
    trading_account_id: int | None = None
    custom_config: dict = Field(default_factory=dict)


class StrategyConfigUpdateRequest(BaseModel):
    trading_account_id: int | None = None
    custom_config: dict = Field(default_factory=dict)
