from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy import select, text

from app.core.config import SPOT_TESTNET_MAX_ORDER_USDT
from app.models.bot_signal_state import BotSignalState
from app.models.error_log import ErrorLog
from app.models.trading_account import TradingAccount

DEFAULTS = {"enabled": False, "max_order_usdt": str(SPOT_TESTNET_MAX_ORDER_USDT), "capital_usdt": "10000"}


class OperationsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool
    max_order_usdt: Decimal = Field(gt=0, le=SPOT_TESTNET_MAX_ORDER_USDT, decimal_places=2)
    capital_usdt: Decimal = Field(gt=0, le=1000000, decimal_places=2)


def controls(runtime):
    return {**DEFAULTS, **runtime.get("v12_controls", {})}


def safety_reason(settings, cash, owned, price, *, mismatched=False):
    if mismatched:
        return "현물 체결 원장과 거래소 BTC 잔고가 일치하지 않습니다."
    if cash + owned * price <= Decimal(settings["capital_usdt"]) * Decimal(".60"):
        return "투입금 대비 평가액 40% 하락으로 정지했습니다."
    return None


def validate_intent(execution, decision, cap):
    if not decision.get("request") or execution.get("decision_id") != decision.get("decision_id"):
        return "저장된 봇 결정에 없는 주문 의도입니다."
    if Decimal(execution["notional"]) > Decimal(cap):
        return "1회 주문 상한을 초과했습니다."
    return None


def halt(db, account, state, reason):
    account.is_running = False
    value = controls(state.runtime_state or {})
    value.update(stop_reason=reason, stopped_at=datetime.now(timezone.utc).isoformat())
    state.runtime_state = {**(state.runtime_state or {}), "v12_controls": value}
    db.add(ErrorLog(user_id=account.user_id, trading_account_id=account.id, source="spot_v12_safety",
                    level="WARNING", message=reason))
    db.commit()
    return {"status": "stopped", "reason": reason}


def get_operations(db, account_id):
    account = db.get(TradingAccount, account_id)
    if account is None or account.market_type != "SPOT":
        raise HTTPException(404, "현물 계정을 찾을 수 없습니다.")
    state = db.scalar(select(BotSignalState).where(BotSignalState.trading_account_id == account_id))
    return account, state


def change_operations(db, account_id, admin_id, *, body=None, resume=False):
    account, state = get_operations(db, account_id)
    if not db.scalar(text("SELECT pg_try_advisory_xact_lock(571126, :key)"), {"key": account.api_key_id}):
        raise HTTPException(409, "계정 실행 중입니다. 다시 시도하세요.")
    db.refresh(account)
    state = db.scalar(select(BotSignalState).where(BotSignalState.trading_account_id == account_id)
                      .execution_options(populate_existing=True))
    if state is None:
        state = BotSignalState(trading_account_id=account_id, runtime_state={})
        db.add(state)
    runtime = dict(state.runtime_state or {})
    previous = controls(runtime)
    changed = dict(previous)
    if resume:
        if runtime.get("v12_wa", {}).get("decision", {}).get("execution", {}).get("status") in {"pending", "unconfirmed"}:
            raise HTTPException(409, "미확정 주문을 먼저 확인해야 합니다.")
        changed.update(enabled=True, stop_reason=None, stopped_at=None)
    else:
        changed.update(enabled=body.enabled, max_order_usdt=format(body.max_order_usdt, "f"),
                       capital_usdt=format(body.capital_usdt, "f"))
        if body.max_order_usdt > body.capital_usdt:
            raise HTTPException(422, "1회 주문 상한은 투입 금액 이하여야 합니다.")
        v12 = runtime.get("v12_wa")
        if v12 and v12.get("paper"):
            book = dict(v12["paper"])
            cash = Decimal(book["cash"]) + body.capital_usdt - Decimal(previous["capital_usdt"])
            if cash < 0:
                raise HTTPException(422, "이미 모의 보유분에 사용한 금액보다 낮출 수 없습니다.")
            book["cash"] = str(cash)
            runtime["v12_wa"] = {**v12, "paper": book}
    audit = {"admin_id": admin_id, "at": datetime.now(timezone.utc).isoformat(),
             "before": previous, "after": changed}
    runtime.update(v12_controls=changed, v12_operations_audit=[*runtime.get("v12_operations_audit", []), audit])
    state.runtime_state = runtime
    account.is_running = bool(changed["enabled"] and not changed.get("stop_reason") and account.is_active)
    db.commit()
    return account, changed
