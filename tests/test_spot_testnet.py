import os

import pytest

from app.services.spot_service import SpotExchange


def test_spot_testnet_account_and_open_orders(allow_spot_testnet_reads):
    key = os.getenv("SPOT_TESTNET_API_KEY")
    secret = os.getenv("SPOT_TESTNET_API_SECRET")
    if not key or not secret:
        pytest.skip("Spot Testnet credentials were not supplied")

    exchange = SpotExchange(key, secret)
    try:
        balances = exchange.account()["balances"]
        order_lists = exchange.open_order_lists()
    except Exception as exc:
        pytest.fail(f"Spot Testnet read failed ({type(exc).__name__})", pytrace=False)
    assert isinstance(balances, list)
    assert isinstance(order_lists, list)
