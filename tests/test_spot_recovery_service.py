from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.spot_recovery_service import inspect_recovery, record_order_fills


def test_reconcile_uses_bot_owned_fills_and_filters_order_lists():
    fills = [{
        "trade_id": 100, "side": "BUY", "quantity": "0.02",
        "commission": "0.001", "commission_asset": "BTC",
    }]
    db = SimpleNamespace(execute=lambda _: SimpleNamespace(
        mappings=lambda: SimpleNamespace(all=lambda: fills),
    ))
    exchange = SimpleNamespace(
        account=lambda: {"balances": [{"asset": "BTC", "free": "1", "locked": "0"}]},
        open_order_lists=lambda: [{"symbol": "ETHUSDT"}, {"symbol": "BTCUSDT"}],
    )
    recovery, orders = inspect_recovery(db, 1, exchange)
    assert recovery["quantity"] == Decimal("0.019")
    assert recovery["alert"]
    assert orders == [{"symbol": "BTCUSDT"}]


def test_recording_rejects_trade_from_different_order():
    with pytest.raises(ValueError):
        record_order_fills(None, 1, 5, [{"orderId": 6}])


def test_recording_makes_exchange_trade_replay_idempotent():
    statements = []
    db = SimpleNamespace(execute=statements.append)
    record_order_fills(db, 1, 5, [{
        "orderId": 5, "id": 9, "isBuyer": True, "qty": "0.02",
        "commission": "0.001", "commissionAsset": "BTC",
    }])
    assert len(statements) == 1
    compiled = statements[0].compile()
    assert "ON CONFLICT DO NOTHING" in str(compiled)
    assert compiled.params["trade_id"] == 9
    assert compiled.params["commission_asset"] == "BTC"
