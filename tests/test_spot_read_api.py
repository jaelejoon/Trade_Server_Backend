from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.routers import trading


@pytest.fixture
def read_api(monkeypatch):
    app = FastAPI()
    app.include_router(trading.router)
    db = Mock()
    account = SimpleNamespace(id=3, user_id=8, api_key_id=5, symbol="BTCUSDT", is_running=False)

    def owned(db, user_id, account_id):
        assert user_id == 8
        if account_id != account.id:
            raise HTTPException(404)
        return account

    monkeypatch.setattr(trading, "get_trading_account_owned_by_user", owned)
    app.dependency_overrides[trading.get_db] = lambda: db
    app.dependency_overrides[trading.get_current_user] = lambda: SimpleNamespace(id=8)
    return TestClient(app), db, account


def test_fills_cursor_precision_and_owner(read_api):
    client, db, _ = read_api
    rows = [SimpleNamespace(trade_id=i, order_id=12, side="BUY",
                            quantity=Decimal("0.00000001"), commission=Decimal("0.00000001"),
                            commission_asset="BTC") for i in (9, 8)]
    query = db.query.return_value
    page = query.filter.return_value.filter.return_value.order_by.return_value
    page.limit.return_value.all.return_value = rows

    data = client.get("/trading/accounts/3/fills?limit=1&before_trade_id=10").json()
    assert data["items"][0]["trade_id"] == "9"
    assert data["items"][0]["quantity"] == "0.00000001"
    assert data["next_before_trade_id"] == "9"
    page.limit.assert_called_once_with(2)
    assert client.get("/trading/accounts/3/fills?limit=101").status_code == 422
    assert client.get("/trading/accounts/4/fills").status_code == 404
    db.commit.assert_not_called()


def test_oco_only_reads_bot_journal_order(read_api, monkeypatch):
    client, db, _ = read_api
    monkeypatch.setattr(trading, "_spot_journal", lambda *_: {"oco": "bot-01", "stop": "90", "target": "120"})
    monkeypatch.setattr(trading, "get_api_key_owned_by_user", Mock())
    monkeypatch.setattr(trading, "get_decrypted_api_keys", lambda _: ("", ""))
    exchange = Mock()
    monkeypatch.setattr(trading, "SpotExchange", lambda *_: exchange)
    leg = {"orderListId": 7, "symbol": "BTCUSDT", "side": "SELL", "origQty": "0.01"}
    exchange.lookup.side_effect = [
        {"symbol": "BTCUSDT", "listClientOrderId": "bot-01", "orderListId": 7,
         "listOrderStatus": "EXECUTING", "orders": [{"clientOrderId": "stop"}, {"clientOrderId": "target"}]},
        {**leg, "type": "STOP_LOSS", "stopPrice": "89.9"},
        {**leg, "type": "LIMIT_MAKER", "price": "119.9"},
    ]
    data = client.get("/trading/accounts/3/oco").json()["oco"]
    assert data == {"source": "bot_journal", "client_order_list_id": "bot-01", "order_list_id": "7",
                    "stop_price": "89.9", "take_profit_price": "119.9", "quantity": "0.01",
                    "status": "EXECUTING"}
    assert exchange.lookup.call_args_list[0].kwargs == {"order_list": True}
    exchange.open_order_lists.assert_not_called()
    exchange.order.assert_not_called()
    db.commit.assert_not_called()


def test_empty_oco_and_recorded_stop(read_api, monkeypatch):
    client, db, account = read_api
    monkeypatch.setattr(trading, "_spot_journal", lambda *_: {})
    key_lookup = Mock()
    monkeypatch.setattr(trading, "get_api_key_owned_by_user", key_lookup)
    assert client.get("/trading/accounts/3/oco").json()["oco"] is None
    assert client.get("/trading/accounts/4/oco").status_code == 404
    key_lookup.assert_not_called()

    db.query.return_value.filter.return_value.order_by.return_value.first.return_value = (
        SimpleNamespace(message="봇 체결 기록 확인 필요", created_at=datetime(2026, 11, 26))
    )
    assert trading._stored_stop(db, account) == (
        "봇 체결 기록 확인 필요", datetime(2026, 11, 26, tzinfo=timezone.utc),
    )
    account.is_running = True
    assert trading._stored_stop(db, account) == (None, None)
    db.commit.assert_not_called()
