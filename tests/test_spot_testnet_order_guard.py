import runpy
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import HTTPException

from app.services import binance_service as exchange

CONFIG = Path(__file__).resolve().parents[1] / "app/core/config.py"
TESTNET = "https://testnet.binance.vision"


@pytest.mark.parametrize(
    ("changes", "hostname", "allowed"),
    [
        ({}, "trade-staging-api", True),
        ({"ENABLE_SPOT_TESTNET_ORDERS": "false"}, "production", False),
        ({"ENVIRONMENT": "prod"}, "trade-staging-api", None),
        ({}, "trade-backtest-api", None),
        ({"BINANCE_TESTNET": "false", "BINANCE_SPOT_BASE_URL": "https://api.binance.com"}, "trade-staging-api", None),
        ({"BINANCE_SPOT_BASE_URL": "https://fapi.binance.com"}, "trade-staging-api", None),
        ({"BINANCE_SPOT_BASE_URL": "https://example.org"}, "trade-staging-api", None),
        ({"BINANCE_FUTURES_BASE_URL": "https://demo-fapi.binance.com"}, "trade-staging-api", None),
        ({"DRY_RUN": "false"}, "trade-staging-api", None),
        ({"ENABLE_LIVE_TRADING": "true"}, "trade-staging-api", None),
        ({"DATABASE_URL": "postgresql://other/database"}, "trade-staging-api", None),
        ({"SPOT_TESTNET_MAX_ORDER_USDT": "20.01"}, "trade-staging-api", None),
    ],
)
def test_startup_matrix(monkeypatch, changes, hostname, allowed):
    env = {
        "ENVIRONMENT": "staging",
        "BINANCE_TESTNET": "true",
        "BINANCE_SPOT_BASE_URL": TESTNET,
        "DRY_RUN": "true",
        "ENABLE_LIVE_TRADING": "false",
        "DATABASE_URL": "postgresql+psycopg2://trade_staging@staging-db:5432/trade_staging",
        "ENABLE_SPOT_TESTNET_ORDERS": "true",
        "SPOT_TESTNET_MAX_ORDER_USDT": "12",
    }
    monkeypatch.delenv("BINANCE_FUTURES_BASE_URL", raising=False)
    for name, value in (env | changes).items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("socket.gethostname", lambda: hostname)
    if allowed is None:
        with pytest.raises(RuntimeError):
            runpy.run_path(str(CONFIG))
    else:
        assert runpy.run_path(str(CONFIG))["ENABLE_SPOT_TESTNET_ORDERS"] is allowed


@pytest.mark.parametrize(
    ("enabled", "testnet", "url", "quote", "expected"),
    [
        (True, True, TESTNET, "12", 200),
        (False, True, TESTNET, "12", 403),
        (True, False, "https://api.binance.com", "12", 403),
        (True, True, "https://fapi.binance.com", "12", 403),
        (True, True, TESTNET, "12.01", 400),
        (True, True, TESTNET, "NaN", 400),
        (True, True, TESTNET, None, 400),
    ],
)
def test_http_write_is_denied_before_signing(monkeypatch, enabled, testnet, url, quote, expected):
    monkeypatch.setattr(exchange, "ENABLE_SPOT_TESTNET_ORDERS", enabled)
    monkeypatch.setattr(exchange, "BINANCE_TESTNET", testnet)
    monkeypatch.setattr(exchange, "BINANCE_SPOT_BASE_URL", url)
    monkeypatch.setattr(exchange, "SPOT_TESTNET_MAX_ORDER_USDT", Decimal("12"))
    post = Mock(return_value=Mock(status_code=200, json=Mock(return_value={"status": "FILLED"})))
    monkeypatch.setattr(exchange.requests, "post", post)
    params = {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET"}
    params.update({"quoteOrderQty": quote} if quote is not None else {"quantity": "1"})

    def send():
        return exchange.binance_signed_request("POST", url, "/api/v3/order", "", "", params)

    if expected == 200:
        assert send() == {"status": "FILLED"}
        target = urlsplit(post.call_args.args[0])
        assert target.hostname == "testnet.binance.vision"
        assert parse_qs(target.query)["quoteOrderQty"] == ["12"]
        assert post.call_args.kwargs["allow_redirects"] is False
    else:
        with pytest.raises(HTTPException) as exc:
            send()
        assert exc.value.status_code == expected
        post.assert_not_called()
