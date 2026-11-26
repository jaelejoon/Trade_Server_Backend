import json
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.core.security import now_utc, to_utc_isoformat
from app.models.latest_signal_state import LatestSignalState


def _json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


def _json_loads(value: Optional[str]) -> Any:
    if not value:
        return None

    try:
        return json.loads(value)
    except Exception:
        return None


def _extract_signal_summary(response: Dict[str, Any]) -> Dict[str, Any]:
    signal = response.get("signal") if isinstance(response, dict) else None

    if not isinstance(signal, dict):
        signal = {}

    return {
        "status": response.get("status") if isinstance(response, dict) else None,
        "message": response.get("message") if isinstance(response, dict) else None,
        "signal_time": signal.get("signal_time"),
        "strategy": signal.get("strategy"),
        "side": signal.get("side"),
    }


def upsert_latest_signal_state(
    db: Session,
    user_id: int,
    trading_account_id: int,
    symbol: str,
    signal_response: Optional[Dict[str, Any]] = None,
    tick_response: Optional[Dict[str, Any]] = None,
    error_message: Optional[str] = None,
) -> LatestSignalState:
    symbol = symbol.upper()
    now = now_utc()

    state = (
        db.query(LatestSignalState)
        .filter(LatestSignalState.trading_account_id == trading_account_id)
        .first()
    )

    if state is None:
        state = LatestSignalState(
            user_id=user_id,
            trading_account_id=trading_account_id,
            symbol=symbol,
        )
        db.add(state)

    state.user_id = user_id
    state.trading_account_id = trading_account_id
    state.symbol = symbol
    state.updated_at = now

    if signal_response is not None:
        summary = _extract_signal_summary(signal_response)
        state.status = summary["status"]
        state.message = summary["message"]
        state.signal_time = summary["signal_time"]
        state.strategy = summary["strategy"]
        state.side = summary["side"]
        state.signal_json = _json_dumps(signal_response)
        state.calculated_at = now
        state.error_message = None

    if tick_response is not None:
        state.tick_json = _json_dumps(tick_response)

    if error_message is not None:
        state.status = "error"
        state.message = error_message[:255]
        state.error_message = error_message
        state.calculated_at = now

    db.commit()
    db.refresh(state)
    return state


def serialize_latest_signal_state(state: Optional[LatestSignalState]) -> Optional[Dict[str, Any]]:
    if state is None:
        return None

    return {
        "id": state.id,
        "user_id": state.user_id,
        "trading_account_id": state.trading_account_id,
        "symbol": state.symbol,
        "status": state.status,
        "message": state.message,
        "signal_time": state.signal_time,
        "strategy": state.strategy,
        "side": state.side,
        "signal_response": _json_loads(state.signal_json),
        "tick_response": _json_loads(state.tick_json),
        "error_message": state.error_message,
        "calculated_at": to_utc_isoformat(state.calculated_at),
        "updated_at": to_utc_isoformat(state.updated_at),
    }


def get_latest_signal_state_by_account(
    db: Session,
    user_id: int,
    trading_account_id: int,
) -> Optional[LatestSignalState]:
    return (
        db.query(LatestSignalState)
        .filter(
            LatestSignalState.user_id == user_id,
            LatestSignalState.trading_account_id == trading_account_id,
        )
        .first()
    )


def get_latest_signal_state_by_symbol(
    db: Session,
    user_id: int,
    symbol: str,
) -> Optional[LatestSignalState]:
    return (
        db.query(LatestSignalState)
        .filter(
            LatestSignalState.user_id == user_id,
            LatestSignalState.symbol == symbol.upper(),
        )
        .order_by(LatestSignalState.updated_at.desc(), LatestSignalState.id.desc())
        .first()
    )
