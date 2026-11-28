from dataclasses import asdict
from datetime import timedelta
from time import monotonic

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from app.services.backtest_engine import BacktestInputError
from app.services.spot_v12 import DEFAULT_PARAMETERS, VERSION, V12Parameters, _shadow_trade, advance
from app.services.spot_v12_runtime import DAILY_MARKS, FIRST_DAY


class V12Request(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    average_months: int = Field(DEFAULT_PARAMETERS.average_months, ge=2, le=24)
    volatility_months: int = Field(DEFAULT_PARAMETERS.volatility_months, ge=2, le=23)
    maximum_weight: float = Field(DEFAULT_PARAMETERS.maximum_weight, ge=0, le=1)
    target_volatility: float = Field(DEFAULT_PARAMETERS.target_volatility, gt=0, le=3)
    defensive_volatility: float = Field(DEFAULT_PARAMETERS.defensive_volatility, gt=0, le=3)
    defensive_maximum: float = Field(DEFAULT_PARAMETERS.defensive_maximum, ge=0, le=1)
    score_gap: float = Field(DEFAULT_PARAMETERS.score_gap, ge=0, le=1)
    maximum_reduction: float = Field(DEFAULT_PARAMETERS.maximum_reduction, ge=0, le=1)
    hold_days: int = Field(DEFAULT_PARAMETERS.hold_days, ge=0, le=365)
    minimum_days: int = Field(DEFAULT_PARAMETERS.minimum_days, ge=1, le=1000)
    minimum_observations: int = Field(DEFAULT_PARAMETERS.minimum_observations, ge=1, le=1000)
    drawdown_penalty: float = Field(DEFAULT_PARAMETERS.drawdown_penalty, ge=0, le=2)


def simulate_v12(db, user_id, body):
    deadline = monotonic() + 120
    db.execute(text("SET LOCAL statement_timeout = '110s'"))
    start, end = body.start.date(), body.end.date()
    rows = db.execute(DAILY_MARKS, {
        "first": FIRST_DAY, "last": end, "user_id": user_id,
        "closed_before": body.end.replace(tzinfo=None),
    }).mappings().all()
    db.rollback()
    params = V12Parameters(**(body.v12_parameters or V12Request()).model_dump())
    fee, slip = body.fee_bps / 10000, body.slippage_bps / 10000
    book = {"cash": body.initial_equity_usdt, "quantity": 0.}
    state, trades, curve, decisions = None, [], [], []
    total_fees = 0.
    peak, drawdown = body.initial_equity_usdt, 0.
    available = [row["day"] for row in rows if row["close"] is not None]
    for row in rows:
        if monotonic() > deadline:
            raise BacktestInputError("V12 백테스트 실행 시간이 초과되었습니다.")
        day = row["day"]
        state, decision = advance(state, day, row["close"], row["exact_close"], row["open"], fee, params, slip)
        if day < start:
            continue
        if row["close"] is not None and day > start:
            equity = book["cash"] + book["quantity"] * row["close"]
            peak = max(peak, equity)
            drawdown = max(drawdown, 1 - equity / peak)
            curve.append({"time": f"{day}T00:00:00Z", "equity_usdt": equity})
        if day == end:
            break
        if decision:
            decisions.append(decision)
        if day == start and state["primary"] is None:
            raise BacktestInputError("시작일 이전 V12 월말 이력이 부족합니다.")
        target = (decision or {}).get("target")
        if day == start and target is None:
            target = state["primary"]
            if state["mode"] == "CASH":
                target = max(0., target - params.maximum_reduction)
            elif state["mode"] == "V35-45":
                target = max(target - params.maximum_reduction, min(target, (target + state["defensive"]) / 2))
        if (day == start or (decision and decision["request"])) and target is not None and row["open"] is not None:
            fill = _shadow_trade(book, row["open"], target, fee, slip)
            if fill:
                price, quantity, side = fill
                commission = price * quantity * fee
                total_fees += commission
                trades.append({"time": f"{day}T00:00:00Z", "side": side,
                               "price": price, "quantity": quantity, "fees_usdt": commission})
    if not curve or curve[-1]["time"] != f"{end}T00:00:00Z":
        raise BacktestInputError("종료일 평가에 필요한 현물 봉이 없습니다.")
    equity = curve[-1]["equity_usdt"]
    return {"engine_version": VERSION, "execution_model": "utc_daily_spot_mark_to_market",
            "parameters": asdict(params), "fee_bps": body.fee_bps, "slippage_bps": body.slippage_bps,
            "period": {"start": start.isoformat(), "end_exclusive": end.isoformat()},
            "data_range": {"first_day": (min(available) - timedelta(days=1)).isoformat(),
                           "last_day": (max(available) - timedelta(days=1)).isoformat(),
                           "observations": len(available)},
            "metrics": {"final_equity_usdt": equity, "net_return_pct": 100 * (equity / body.initial_equity_usdt - 1),
                        "mdd_pct": 100 * drawdown, "trade_count": len(trades), "fees_usdt": total_fees},
            "trades": trades, "equity_curve": curve, "decisions": decisions}
