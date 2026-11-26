from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import HTTPException
from pydantic import ValidationError
import pytest

from app.schemas import FuturesMarketOrderRequest, SetLeverageRequest, TradingAccountCreateRequest
from app.services import binance_service as exchange
from app.services import bot_service as bot
from app.services.trading_strategy_service import create_admin_strategy, validate_strategy_config


@pytest.mark.parametrize('model,fields', [
    (TradingAccountCreateRequest, {}),
    (SetLeverageRequest, {'leverage': 2}),
    (FuturesMarketOrderRequest, {'side': 'BUY', 'quantity': 1}),
])
def test_trading_requests_reject_other_symbols(model, fields):
    with pytest.raises(ValidationError, match='BTCUSDT'):
        model(api_key_id=1, symbol='ETHUSDT', **fields)
    assert model(api_key_id=1, symbol=' btcusdt ', **fields).symbol == 'BTCUSDT'


@pytest.mark.parametrize('model', [TradingAccountCreateRequest, SetLeverageRequest])
@pytest.mark.parametrize('leverage', [1, 2, 3])
def test_leverage_request_cap(model, leverage):
    if leverage == 3:
        with pytest.raises(ValidationError, match='less than or equal to 2'):
            model(api_key_id=1, leverage=leverage)
    else:
        assert model(api_key_id=1, leverage=leverage).leverage == leverage


@pytest.mark.parametrize('leverage', [1, 2, 3])
def test_strategy_setting_leverage_cap(leverage):
    strategy = SimpleNamespace(default_config={'LIQUIDATION_LEVERAGE': 2.0}, editable_fields=['LIQUIDATION_LEVERAGE'])
    config = {'LIQUIDATION_LEVERAGE': leverage}
    if leverage == 3:
        with pytest.raises(HTTPException, match='2배'):
            validate_strategy_config(strategy, config)
        db = Mock()
        with pytest.raises(HTTPException, match='2배'):
            create_admin_strategy(db, 1, 'custom', 'custom', '1', None, config, None)
        db.add.assert_not_called()
    else:
        assert validate_strategy_config(strategy, config) == config


@pytest.mark.parametrize('path', ['/fapi/v1/order', '/fapi/v1/algoOrder', '/fapi/v1/leverage'])
def test_signed_writes_reject_other_symbols(monkeypatch, path):
    monkeypatch.setattr(exchange, 'BINANCE_TESTNET', False)
    monkeypatch.setattr(exchange, 'DRY_RUN', False)
    monkeypatch.setattr(exchange, 'ENABLE_LIVE_TRADING', True)
    send = Mock()
    monkeypatch.setattr(exchange.requests, 'post', send)
    with pytest.raises(HTTPException, match='BTCUSDT'):
        exchange.binance_signed_request('POST', exchange.BINANCE_FUTURES_BASE_URL, path, 'k', 's', {'symbol': 'ETHUSDT', 'leverage': 2})
    send.assert_not_called()


def test_signed_leverage_write_rejects_three(monkeypatch):
    monkeypatch.setattr(exchange, 'BINANCE_TESTNET', False)
    monkeypatch.setattr(exchange, 'DRY_RUN', False)
    monkeypatch.setattr(exchange, 'ENABLE_LIVE_TRADING', True)
    send = Mock()
    monkeypatch.setattr(exchange.requests, 'post', send)
    with pytest.raises(HTTPException, match='2배'):
        exchange.futures_set_leverage_raw('k', 's', 'BTCUSDT', 3)
    send.assert_not_called()


@pytest.mark.parametrize('symbol,leverage,message', [
    ('ETHUSDT', 2, 'BTCUSDT만'),
    ('BTCUSDT', 5, '거래 계정 설정'),
])
def test_legacy_account_blocks_new_entry(monkeypatch, symbol, leverage, message):
    state = SimpleNamespace(runtime_state={})
    monkeypatch.setattr(bot, 'get_api_key_owned_by_user', Mock())
    monkeypatch.setattr(bot, 'get_decrypted_api_keys', lambda _: ('k', 's'))
    monkeypatch.setattr(bot, 'reconcile_position_and_open_orders', lambda **_: {'active_position': None})
    monkeypatch.setattr(bot.BinanceCandleSyncService, 'sync_recent_1m_candles', Mock())
    monkeypatch.setattr(bot, 'get_or_create_bot_state', lambda *_: state)
    monkeypatch.setattr(bot, 'manage_position', lambda *_: ({}, None))
    monkeypatch.setattr(bot, '_save_latest_signal_state_safe', Mock())
    submit = Mock()
    monkeypatch.setattr(bot, 'place_market_with_bracket', submit)
    account = SimpleNamespace(id=1, user_id=1, api_key_id=1, symbol=symbol, leverage=leverage, dry_run=True)
    result = bot._execute_bot_tick(Mock(), account)
    assert result['status'] == 'blocked'
    assert message in result['message']
    submit.assert_not_called()


@pytest.mark.parametrize('leverage', [1, 2, 3, 5])
@pytest.mark.parametrize('testnet', [False, True])
def test_exchange_leverage_cap_before_automatic_entry(monkeypatch, leverage, testnet):
    monkeypatch.setattr(bot, 'BINANCE_TESTNET', testnet)
    monkeypatch.setattr(bot, 'is_account_live_trading_enabled', lambda *_: True)
    monkeypatch.setattr(bot, 'get_futures_symbol_config', lambda *_: [{'symbol': 'BTCUSDT', 'leverage': leverage}])
    monkeypatch.setattr(bot, 'normalize_price', lambda _, value: value)
    change_leverage = Mock()
    submit = Mock(return_value={'executedQty': '1', 'avgPrice': '100'})
    monkeypatch.setattr(bot, 'futures_set_leverage_raw', change_leverage)
    monkeypatch.setattr(bot, 'futures_market_order_raw', submit)
    monkeypatch.setattr(bot, 'futures_stop_market_order_raw', Mock(return_value={'algoId': 1}))
    monkeypatch.setattr(bot, 'futures_take_profit_market_order_raw', Mock(return_value={'algoId': 2}))
    account = SimpleNamespace(id=1, leverage=min(leverage, 2))
    result = bot.place_market_with_bracket(Mock(), account, 'k', 's', 'BTCUSDT', 'BUY', 'SELL', 1, 99., 108.5, 100.)
    if leverage <= 2:
        assert result['status'] == 'SUBMITTED'
        submit.assert_called_once()
    else:
        assert result['status'] == 'ENTRY_BLOCKED'
        assert '2배' in result['message']
        submit.assert_not_called()
    change_leverage.assert_not_called()
