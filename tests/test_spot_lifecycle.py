from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from app.services import spot_lifecycle as module
from app.services.binance_service import OrderExecutionUnknown


def order(side, qty, order_id=1, status="FILLED"):
    return {"side": side, "orderId": order_id, "executedQty": qty, "status": status}


@pytest.fixture
def setup(monkeypatch):
    db, exchange = Mock(), Mock()
    account = SimpleNamespace(id=1, user_id=2, is_running=True)
    state = SimpleNamespace(runtime_state={})
    recovery = {"halt": False, "quantity": Decimal(".019"), "owned": Decimal(".019"),
                "balance": Decimal(".030"), "alert": None}
    inspect = Mock(return_value=(recovery, []))
    persist = Mock()
    monkeypatch.setattr(module, "inspect_recovery", inspect)
    monkeypatch.setattr(module, "record_order_fills", persist)
    monkeypatch.setattr(module, "create_error_log", Mock())
    bot = module.SpotLifecycle(db, account, state, exchange)
    bot.save(stop="90", target="120")
    return bot, exchange, recovery, inspect, persist


@pytest.mark.parametrize("exit_type", ["STOP_LOSS", "LIMIT_MAKER"])
def test_entry_and_oco_exit(setup, exit_type):
    bot, exchange, recovery, inspect, persist = setup
    exchange.order.return_value = order("BUY", ".020")
    exchange.trades.return_value = [{"qty": ".020", "isBuyer": True}]
    assert bot.entry("closed", ".020", "90", "120")["status"] == "protected"
    persist.assert_called_once()
    assert exchange.protect.call_args.args[:3] == (Decimal(".019"), "90", "120")

    restart = module.SpotLifecycle(bot.db, bot.account, bot.state, exchange)
    exit_id = restart.journal["oco"]
    exchange.lookup.side_effect = [
        {"listOrderStatus": "ALL_DONE", "orders": [{"clientOrderId": exit_type}]},
        order("SELL", ".019", 2),
    ]
    exchange.trades.return_value = [{"qty": ".019", "isBuyer": False}]
    recovery.update(quantity=Decimal(0), owned=Decimal(0))
    assert restart.advance()["status"] == "flat"
    assert restart.journal["oco"] is None
    assert exchange.lookup.call_args_list[0].args == (exit_id,)
    assert persist.call_count == 2


def test_testnet_entry_submits_quote_and_protects_bot_quantity(setup):
    bot, exchange, _, _, persist = setup
    exchange.market_buy_quote.return_value = order("BUY", ".020")
    exchange.trades.return_value = [{"qty": ".020", "isBuyer": True}]

    assert bot.entry("spot-testnet-check", Decimal("12"), "90", "120", quote=True)["status"] == "protected"

    assert exchange.market_buy_quote.call_args.args[0] == Decimal("12")
    exchange.order.assert_not_called()
    assert exchange.protect.call_args.args[:3] == (Decimal(".019"), "90", "120")
    persist.assert_called_once()


def test_rejected_oco_sells_only_bot_quantity_then_stops(setup):
    bot, exchange, _, _, persist = setup
    exchange.protect.side_effect = HTTPException(400)
    exchange.order.return_value = order("SELL", ".019")
    exchange.trades.return_value = [{"qty": ".019", "isBuyer": False}]
    assert bot.advance()["status"] == "stopped"
    assert exchange.order.call_args.args[:2] == ("SELL", Decimal(".019"))
    assert bot.account.is_running is False
    persist.assert_called_once()


def test_ambiguous_oco_stops_without_selling(setup):
    bot, exchange, _, _, _ = setup
    exchange.protect.side_effect = OrderExecutionUnknown
    assert bot.advance()["status"] == "stopped"
    exchange.order.assert_not_called()
    assert bot.journal["oco"]


def test_restart_keeps_existing_oco(setup):
    bot, exchange, recovery, inspect, _ = setup
    bot.save(oco="own-list")
    exchange.lookup.return_value = {"orders": [], "listOrderStatus": "EXECUTING"}
    inspect.return_value = recovery, [{"listClientOrderId": "own-list"}]
    assert bot.advance()["status"] == "protected"
    exchange.protect.assert_not_called()
    exchange.order.assert_not_called()


def test_restart_re_registers_missing_protection(setup):
    bot, exchange, _, _, _ = setup
    assert bot.advance()["status"] == "protected"
    exchange.protect.assert_called_once()
    exchange.order.assert_not_called()


def test_restart_unknown_ownership_halts_without_mutations(setup):
    bot, exchange, recovery, _, _ = setup
    recovery["halt"] = True
    assert bot.advance()["status"] == "stopped"
    exchange.order.assert_not_called()
    exchange.protect.assert_not_called()


def test_restart_queries_pending_buy_by_journal_id(setup):
    bot, exchange, _, _, _ = setup
    bot.save(buy="existing-buy")
    exchange.lookup.return_value = order("BUY", ".020")
    exchange.trades.return_value = [{"qty": ".020", "isBuyer": True}]
    assert bot.advance()["status"] == "protected"
    exchange.lookup.assert_called_once_with("existing-buy")
    exchange.order.assert_not_called()
