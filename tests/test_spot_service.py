from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.services import spot_service as spot

FILTERS = {
    "LOT_SIZE": {"stepSize": "0.001", "minQty": "0.001", "maxQty": "10"},
    "PRICE_FILTER": {"tickSize": "0.1", "minPrice": "1", "maxPrice": "1000000"},
    "MIN_NOTIONAL": {"minNotional": "10", "applyToMarket": True},
}


@pytest.mark.parametrize("side", ["BUY", "SELL"])
@pytest.mark.parametrize("price", [None, "10000.19"])
def test_spot_order_payload(monkeypatch, side, price):
    exchange = spot.SpotExchange("", "")
    monkeypatch.setattr(spot, "_filters", lambda *_: FILTERS)
    monkeypatch.setattr(exchange, "_reference_prices", lambda: ["10000", "9999"])
    monkeypatch.setattr(exchange, "_signed", lambda method, path, params: (method, path, params))
    method, path, params = exchange.order(side, "0.0039", "bot-1", price)
    assert (method, path, params["side"], params["quantity"], params["newClientOrderId"]) == (
        "POST", "/api/v3/order", side, "0.003", "bot-1",
    )
    assert params.get("price") == ("10000.1" if price else None)


def test_no_order_below_minimum(monkeypatch):
    monkeypatch.setattr(spot, "_filters", lambda *_: FILTERS)
    with pytest.raises(ValueError, match="최소 금액"):
        spot.SpotExchange("", "").order("BUY", "0.001", "bot-2", "5000")


@pytest.mark.parametrize("found", [True, False])
def test_oco_unknown_queries_without_resending(monkeypatch, found):
    exchange = spot.SpotExchange("", "")
    monkeypatch.setattr(spot, "_filters", lambda *_: FILTERS)
    calls = []

    def signed(method, path, params):
        calls.append((method, path, params))
        if method == "POST":
            raise spot.OrderExecutionUnknown
        if not found:
            raise HTTPException(400)
        return {"orderListId": 7}

    monkeypatch.setattr(exchange, "_signed", signed)
    if found:
        assert exchange.protect("0.01", "9000", "11000", "oco-1") == {"orderListId": 7}
    else:
        with pytest.raises(spot.OrderExecutionUnknown):
            exchange.protect("0.01", "9000", "11000", "oco-1")
    assert [(method, path) for method, path, _ in calls] == [
        ("POST", "/api/v3/orderList/oco"), ("GET", "/api/v3/orderList")
    ]
    assert calls[1][2] == {"origClientOrderId": "oco-1"}


@pytest.mark.parametrize(("free", "quantity"), [("1", "0.899"), ("0.5", "0.5")])
def test_recovery_caps_quantity_and_alerts(free, quantity):
    fills = [
        {"trade_id": 1, "side": "BUY", "quantity": "1", "commission": "0.001", "commission_asset": "BTC"},
        {"trade_id": 2, "side": "SELL", "quantity": "0.1", "commission": "1", "commission_asset": "USDT"},
    ]
    result = spot.recovery_quantity(fills, free, "0")
    assert result["quantity"] == Decimal(quantity)
    assert result["alert"] and not result["halt"]


@pytest.mark.parametrize("fills", [[], [{"trade_id": 1}], [{"trade_id": 1}] * 2])
def test_unconfirmed_recovery_halts(fills):
    result = spot.recovery_quantity(fills, "1", "0")
    assert result["halt"] and result["quantity"] == 0 and result["alert"]


def test_global_guard_blocks_spot_mutation():
    with pytest.raises(HTTPException) as exc:
        spot.SpotExchange("", "")._signed(
            "POST", "/api/v3/order", {"symbol": spot.SYMBOL},
        )
    assert exc.value.status_code == 403
