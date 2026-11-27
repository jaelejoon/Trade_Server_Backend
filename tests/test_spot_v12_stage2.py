from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.routers.backtest_runs import RunRequest
from app.services.spot_v12 import DEFAULT_PARAMETERS, V12Parameters, monthly_targets
from app.services.spot_v12_controls import OperationsRequest, safety_reason, validate_intent


def test_long_v12_request_is_isolated_from_server_defaults():
    body = RunRequest.model_validate({"strategy_id": "spot-v12-wa-1", "start": "2020-01-01T00:00:00Z",
                                      "end": "2026-11-01T00:00:00Z", "initial_equity_usdt": 10000,
                                      "v12_parameters": {"maximum_weight": .25}})
    assert (body.fee_bps, body.slippage_bps) == (10, 2)
    closes = list(range(100, 112))
    assert monthly_targets(closes, V12Parameters(**body.v12_parameters.model_dump()))[0] == .25
    assert DEFAULT_PARAMETERS.maximum_weight == monthly_targets(closes)[0] == 1
    with pytest.raises(ValidationError):
        RunRequest.model_validate({**body.model_dump(mode="json"), "strategy_id": "V57_LONG_SAFE_PUSH"})


@pytest.mark.parametrize("bad", [{"capital_usdt": "0"}, {"max_order_usdt": "9999"}, {"dry_run": False}])
def test_operations_only_allows_bounded_operational_values(bad):
    with pytest.raises(ValidationError):
        OperationsRequest.model_validate({"enabled": True, "capital_usdt": "1000", "max_order_usdt": "10", **bad})


def test_stop_conditions_at_exact_threshold_and_for_unexpected_orders():
    settings = {"capital_usdt": "100"}
    assert safety_reason(settings, Decimal(60), Decimal(0), Decimal(1))
    assert safety_reason(settings, Decimal("60.01"), Decimal(0), Decimal(1)) is None
    assert safety_reason(settings, Decimal(200), Decimal(0), Decimal(1), mismatched=True)
    decision = {"request": True, "decision_id": "d"}
    order = {"side": "BUY", "quantity": ".001", "quote": "12", "notional": "12", "decision_id": "d"}
    assert validate_intent({**order, "decision_id": "other"}, decision, "12")
    assert validate_intent({**order, "quote": "12.01"}, decision, "12")
    assert validate_intent(order, decision, "12") is None
    assert validate_intent({**order, "side": "SELL"}, decision, "12", sell_price=Decimal(13000))
