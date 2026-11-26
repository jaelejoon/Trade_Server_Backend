from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest
import requests

from app.strategy import strategy_v57 as strategy
from app.services import binance_service as exchange
from app.services import bot_service as bot
from app.services import position_management_service as management


def signal(time='2026-11-24 10:00:00'):
    return {'time': pd.Timestamp(time), 'timeframe': '1H', 'side': 'LONG',
            'strategy': 'B_1H_BREAKOUT_LONG', 'market_regime': 'NORMAL_BULL',
            'event_risk': False, 'atr': 1., 'rsi': 60., 'h4_adx': 25.,
            'bb_width': .04, 'atr_pct': .01}


def candles(start='2026-11-24 09:45', count=5):
    return pd.DataFrame({'open': 100., 'high': 100.2, 'low': 99.9, 'close': 100.,
                         'm15_atr': 1., 'm15_ema_slow': 100.},
                        index=pd.date_range(start, periods=count, freq='15min'))


def position(**overrides):
    result = {'side': 'LONG', 'entry_price': 100., 'initial_risk': 1.,
              'stop_price': 99., 'take_profit_price': 108.5, 'qty': 1.,
              'max_hold_bars': 96, 'bars_held': 0, 'max_favorable_r': 0.,
              'max_adverse_r': 0., 'liquidation_price': 55.,
              'dry_run': True, 'config': deepcopy(strategy.DEFAULT_V57_CONFIG),
              'entry_time': '2026-11-24T10:00:00', 'last_bar': None,
              'stop_algo_id': 1, 'tp_algo_id': 2}
    result.update(overrides)
    return result


def test_signal_is_available_only_at_hour_close():
    row = signal()
    row.update(entry_signal_1h=True, signal_strategy_1h=row['strategy'])
    events = strategy.build_signal_events(pd.DataFrame([row], index=[pd.Timestamp('2026-11-24 09:00')]), pd.DataFrame())
    assert events.iloc[0]['time'] == pd.Timestamp('2026-11-24 10:00')


@pytest.mark.parametrize('minutes, expected', [(59, 0), (60, 1), (61, 1), (120, 2)])
def test_resampling_excludes_partial_hours(minutes, expected):
    raw = pd.DataFrame({'open': 100., 'high': 101., 'low': 99., 'close': 100., 'volume': 1.},
                       index=pd.date_range('2026-11-24', periods=minutes, freq='min'))
    assert len(strategy.resample_ohlcv(raw, '1h')) == expected


def test_resampling_rejects_missing_minutes():
    raw = pd.DataFrame({'open': 100., 'high': 101., 'low': 99., 'close': 100., 'volume': 1.},
                       index=pd.date_range('2026-11-24', periods=60, freq='min')).drop(pd.Timestamp('2026-11-24 00:15'))
    assert strategy.resample_ohlcv(raw, '1h').empty


def test_entry_bar_stop_is_executed():
    bars = candles()
    bars.loc[pd.Timestamp('2026-11-24 10:00'), 'low'] = 90.
    equity, trades = strategy.backtest(bars, pd.DataFrame([signal()]))
    assert len(trades) == 1
    assert trades.iloc[0]['exit_reason'] == 'STOP'
    assert trades.iloc[0]['entry_time'] == trades.iloc[0]['exit_time']
    assert equity.iloc[-1]['open_positions'] == 0


def test_trailing_stop_not_applied_retroactively():
    p = position()
    row = pd.Series({'open': 100., 'high': 102., 'low': 99.5, 'close': 101.8, 'm15_atr': .2, 'm15_ema_slow': 100.})
    reason, _ = strategy.advance_position(p, row)
    assert reason is None
    assert p['stop_price'] == pytest.approx(101.4)
    reason, price = strategy.advance_position(p, row)
    assert reason == 'TRAIL_STOP'
    assert price == 100.  # gap through the previously active stop


@pytest.mark.parametrize('side,low,high,opened,expected', [('LONG', 95., 101., 96., 96.), ('SHORT', 99., 105., 104., 104.)])
def test_gap_stop_uses_worse_open(side, low, high, opened, expected):
    p = position(side=side, liquidation_price=55. if side == 'LONG' else 145., stop_price=99. if side == 'LONG' else 101., take_profit_price=108. if side == 'LONG' else 92.)
    reason, price = strategy.advance_position(p, {'open': opened, 'high': high, 'low': low, 'close': 100., 'm15_atr': 1., 'm15_ema_slow': 100.})
    assert reason == 'STOP'
    assert price == expected


def test_same_bar_stop_has_priority_over_profit():
    reason, _ = strategy.advance_position(position(), {'open': 100., 'high': 110., 'low': 98., 'close': 105., 'm15_atr': 1., 'm15_ema_slow': 100.})
    assert reason == 'STOP'


@pytest.mark.parametrize('ema,limit,reason', [(102., 96, 'EMA_TREND_EXIT'), (100., 1, 'TIME_EXIT')])
def test_management_exits(ema, limit, reason):
    p = position(max_hold_bars=limit)
    result, _ = strategy.advance_position(p, {'open': 100., 'high': 102., 'low': 99.5, 'close': 101., 'm15_atr': 1., 'm15_ema_slow': ema})
    assert result == reason


def test_only_one_position_matches_server():
    bars = candles(count=8)
    _, trades = strategy.backtest(bars, pd.DataFrame([signal(), signal('2026-11-24 10:15')]))
    equity, _ = strategy.backtest(bars, pd.DataFrame([signal(), signal('2026-11-24 10:15')]))
    assert equity['open_positions'].max() == 1


def test_funding_reduces_equity():
    bars = candles(count=8)
    baseline, _ = strategy.backtest(bars, pd.DataFrame([signal()]))
    funding = [{'fundingTime': int(pd.Timestamp('2026-11-24 10:15').timestamp() * 1000), 'markPrice': '100', 'fundingRate': '.001'}]
    with_funding, _ = strategy.backtest(bars, pd.DataFrame([signal()]), funding_rates=funding)
    assert with_funding.iloc[-1]['equity'] < baseline.iloc[-1]['equity']


def test_unclosed_position_equity_is_reported():
    equity = pd.DataFrame({'equity': [strategy.INITIAL_CAPITAL, strategy.INITIAL_CAPITAL * .9]})
    result = strategy.calculate_stats(pd.DataFrame(), equity)
    assert result['return_pct'] == pytest.approx(-10)


@pytest.mark.parametrize('event_time,now,expected', [('2026-11-24 09:00', '2026-11-24 10:01', False), ('2026-11-24 10:00', '2026-11-24 10:01', True), ('2026-11-24 10:00', '2026-11-24 10:06', False)])
def test_signal_freshness(monkeypatch, event_time, now, expected):
    raw = pd.DataFrame({'timestamp': [pd.Timestamp(now).floor('min') - pd.Timedelta(minutes=1)]})
    monkeypatch.setattr(strategy, 'prepare_data', lambda _: (pd.DataFrame({'x': [1]}), pd.DataFrame({'x': [1]})))
    monkeypatch.setattr(strategy, 'build_signal_events', lambda *_: pd.DataFrame([signal(event_time)]))
    assert strategy.get_latest_v57_signal(raw, now=now).has_signal is expected


def test_stale_feed_rejected():
    raw = pd.DataFrame({'timestamp': [pd.Timestamp('2026-11-24 09:00')]})
    assert strategy.get_latest_v57_signal(raw, now='2026-11-24 10:00').reason == 'STALE_CANDLE_DATA'


@pytest.mark.parametrize('balance,reason', [(95, 'DAILY_DD_GUARD'), (89, 'MONTHLY_DD_GUARD'), (80, 'PEAK_DD_GUARD')])
def test_equity_guards(balance, reason):
    runtime = {}
    management.update_equity_guard(runtime, 100., '2026-11-01')
    # Keep daily baseline above threshold only for the daily-specific case.
    now = '2026-11-01' if reason == 'DAILY_DD_GUARD' else '2026-11-02'
    blocked, actual = management.update_equity_guard(runtime, balance, now)
    assert blocked and actual == reason


def test_override_is_restored_after_failure():
    original = strategy.B_RISK_NORMAL
    with pytest.raises(RuntimeError):
        with strategy.V57ConfigOverride({'B_RISK_NORMAL': .001}):
            raise RuntimeError()
    assert strategy.B_RISK_NORMAL == original


def test_algo_order_payload(monkeypatch):
    send = Mock(return_value={'algoId': 123})
    monkeypatch.setattr(exchange, 'normalize_price', lambda _, p: p)
    monkeypatch.setattr(exchange, 'binance_signed_request', send)
    exchange.futures_stop_market_order_raw('key', 'secret', 'BTCUSDT', 'SELL', 99., 'stable-stop')
    args, kwargs = send.call_args
    assert args[2] == '/fapi/v1/algoOrder'
    assert kwargs['params']['triggerPrice'] == 99.
    assert kwargs['params']['clientAlgoId'] == 'stable-stop'
    assert kwargs['params']['algoType'] == 'CONDITIONAL'


def test_timed_out_post_never_retried(monkeypatch):
    monkeypatch.setattr(exchange, 'DRY_RUN', False)
    monkeypatch.setattr(exchange, 'ENABLE_LIVE_TRADING', True)
    send = Mock(side_effect=requests.Timeout())
    monkeypatch.setattr(exchange.requests, 'post', send)
    with pytest.raises(exchange.OrderExecutionUnknown):
        exchange.binance_signed_request('POST', exchange.BINANCE_FUTURES_BASE_URL, '/fapi/v1/order', 'key', 'secret', {'symbol': 'BTCUSDT'})
    assert send.call_count == 1


def test_unknown_entry_queried_by_same_id(monkeypatch):
    filled = {'status': 'FILLED', 'executedQty': '1', 'avgPrice': '100'}
    send = Mock(side_effect=[exchange.OrderExecutionUnknown(), filled])
    monkeypatch.setattr(exchange, 'binance_signed_request', send)
    assert exchange.futures_market_order_raw('k', 's', 'BTCUSDT', 'BUY', 1, new_client_order_id='same-id') == filled
    assert send.call_args_list[1].args[0] == 'GET'
    assert send.call_args_list[1].kwargs['params']['origClientOrderId'] == 'same-id'


def test_unknown_unresolved_entry_not_resubmitted(monkeypatch):
    send = Mock(side_effect=[exchange.OrderExecutionUnknown(), RuntimeError()])
    monkeypatch.setattr(exchange, 'binance_signed_request', send)
    with pytest.raises(exchange.OrderExecutionUnknown):
        exchange.futures_market_order_raw('k', 's', 'BTCUSDT', 'BUY', 1, new_client_order_id='same-id')
    assert [c.args[0] for c in send.call_args_list] == ['POST', 'GET']


def test_global_dry_run_overrides_user_permission():
    db = Mock()
    db.query.return_value.filter.return_value.first.return_value = SimpleNamespace(is_active=True, enable_live_trading=True)
    assert bot.is_account_live_trading_enabled(db, SimpleNamespace(dry_run=False, user_id=1)) is False


def test_bracket_failure_closes_before_cancelling(monkeypatch):
    monkeypatch.setattr(bot, 'is_account_live_trading_enabled', lambda *_: True)
    leverage_write = Mock()
    monkeypatch.setattr(bot, 'futures_set_leverage_raw', leverage_write)
    monkeypatch.setattr(bot, 'get_futures_symbol_config', lambda *_: [{'symbol': 'BTCUSDT', 'leverage': 2}])
    monkeypatch.setattr(bot, 'get_futures_open_algo_orders_raw', lambda *_: [])
    monkeypatch.setattr(bot, 'futures_market_order_raw', Mock(return_value={'executedQty': '1', 'avgPrice': '100', 'status': 'FILLED'}))
    monkeypatch.setattr(bot, 'normalize_price', lambda _, p: p)
    monkeypatch.setattr(bot, 'futures_stop_market_order_raw', Mock(side_effect=RuntimeError('bracket failed')))
    close = Mock(return_value={'executed': True})
    monkeypatch.setattr(bot, 'emergency_close_position', close)
    monkeypatch.setattr(bot, 'create_error_log', Mock())
    account = SimpleNamespace(id=1, user_id=1, leverage=2, is_running=True)
    result = bot.place_market_with_bracket(Mock(), account, 'k', 's', 'BTCUSDT', 'BUY', 'SELL', 1, 99., 108.5, 100.)
    assert close.call_count == 1
    leverage_write.assert_not_called()
    assert not account.is_running
    assert result['status'] == 'BRACKET_FAILED_EMERGENCY_HANDLED'


def test_paper_management_never_submits_orders(monkeypatch):
    raw = pd.DataFrame({'timestamp': pd.date_range('2026-11-24 10:00', periods=60, freq='min'),
                        'open': 100., 'high': 100.2, 'low': 98., 'close': 100., 'volume': 1.})
    monkeypatch.setattr(management.CandleService, 'load_recent_1m_dataframe', lambda *_a, **_k: raw)
    submit = Mock(side_effect=AssertionError('paper submitted an order'))
    monkeypatch.setattr(management, 'futures_market_order_raw', submit)
    state = SimpleNamespace(runtime_state={'position': position(), 'paper_balance': 1000.})
    account = SimpleNamespace(id=1, user_id=1, symbol='BTCUSDT')
    runtime, result = management.manage_position(None, account, state, 'k', 's', None, False, now='2026-11-24 11:01')
    assert result['status'] == 'closed'
    assert 'position' not in runtime
    assert runtime['paper_balance'] < 1000.
    submit.assert_not_called()


def test_mode_switch_does_not_touch_real_account():
    state = SimpleNamespace(runtime_state={'position': position()})
    _, result = management.manage_position(None, SimpleNamespace(), state, 'k', 's', None, True, now='2026-11-24 11:00')
    assert result['message'] == 'POSITION_MODE_CHANGED'


def test_stop_replacement_creates_before_cancelling(monkeypatch):
    raw = pd.DataFrame({'timestamp': pd.date_range('2026-11-24 09:00', periods=90, freq='min'),
                        'open': 100., 'high': 102., 'low': 99.5, 'close': 101.8, 'volume': 1.})
    monkeypatch.setattr(management.CandleService, 'load_recent_1m_dataframe', lambda *_a, **_k: raw)
    monkeypatch.setattr(management.engine, 'update_trailing_stop', lambda p, _: p.update(stop_price=100.25))
    monkeypatch.setattr(management, 'normalize_price', lambda _, p: p)
    monkeypatch.setattr(management, 'get_futures_open_algo_orders_raw', lambda *_: [{'algoId': 1}, {'algoId': 2}])
    calls = []
    monkeypatch.setattr(management, 'futures_stop_market_order_raw', lambda *_: calls.append('create') or {'algoId': 3})
    monkeypatch.setattr(management, 'cancel_futures_algo_order_raw', lambda *_: calls.append('cancel'))
    state = SimpleNamespace(runtime_state={'position': position(dry_run=False), 'guard': {'peak': 1000}})
    account = SimpleNamespace(id=1, user_id=1, symbol='BTCUSDT')
    runtime, result = management.manage_position(None, account, state, 'k', 's', {'positionAmt': '1'}, True, now='2026-11-24 10:31')
    assert result['status'] == 'holding'
    assert calls == ['create', 'cancel']
    assert runtime['position']['stop_algo_id'] == 3


def test_pending_order_prevents_new_entry(monkeypatch):
    state = SimpleNamespace(runtime_state={'pending_entry': {'plan': {}}})
    monkeypatch.setattr(bot, 'get_api_key_owned_by_user', lambda **_: None)
    monkeypatch.setattr(bot, 'get_decrypted_api_keys', lambda _: ('k', 's'))
    monkeypatch.setattr(bot, 'reconcile_position_and_open_orders', lambda **_: {'active_position': None})
    monkeypatch.setattr(bot.BinanceCandleSyncService, 'sync_recent_1m_candles', Mock())
    monkeypatch.setattr(bot, 'get_or_create_bot_state', lambda *_: state)
    monkeypatch.setattr(bot, 'manage_position', lambda *_: (state.runtime_state, None))
    submit = Mock(side_effect=AssertionError('pending entry was duplicated'))
    monkeypatch.setattr(bot, 'place_market_with_bracket', submit)
    account = SimpleNamespace(id=1, user_id=1, api_key_id=1, symbol='BTCUSDT', dry_run=True)
    result = bot._execute_bot_tick(Mock(), account)
    assert result['message'] == 'ENTRY_RECONCILIATION_REQUIRED'
    submit.assert_not_called()


def test_mdd_includes_initial_capital():
    assert strategy.calculate_mdd_from_equity(pd.Series([strategy.INITIAL_CAPITAL * .9])) == pytest.approx(-10)


def test_api_app_imports_without_contacting_database(monkeypatch):
    import importlib
    from app.db.base import Base
    from app.db import migrations
    monkeypatch.setattr(Base.metadata, 'create_all', Mock())
    monkeypatch.setattr(migrations, 'ensure_runtime_schema', Mock())
    module = importlib.import_module('app.main')
    assert module.root()['dry_run'] is True


def test_future_candles_do_not_change_closed_signals():
    import numpy as np
    rng = np.random.default_rng(57)
    count = 18000
    prices = 100 * np.exp(np.cumsum(rng.normal(.000005, .001, count)))
    raw = pd.DataFrame({'timestamp': pd.date_range('2026-10-01', periods=count, freq='min'),
                        'open': prices, 'high': prices * 1.001, 'low': prices * .999,
                        'close': prices, 'volume': 1.})
    prefix = raw.iloc[:15840]
    full_hours, _ = strategy.prepare_data(raw)
    prefix_hours, _ = strategy.prepare_data(prefix)
    pd.testing.assert_frame_equal(full_hours.loc[prefix_hours.index], prefix_hours)


def test_cooldown_blocks_the_bar_after_exit():
    bars = candles(count=8)
    bars.loc[pd.Timestamp('2026-11-24 10:00'), 'low'] = 90.
    equity, trades = strategy.backtest(bars, pd.DataFrame([signal(), signal('2026-11-24 10:15')]))
    assert len(trades) == 1
    assert equity.iloc[-1]['open_positions'] == 0


@pytest.mark.parametrize('account_dry_run,user_dry_run,expected', [
    (True, False, False), (False, True, False), (False, False, True),
])
def test_account_and_user_dry_run_override_permission(monkeypatch, account_dry_run, user_dry_run, expected):
    monkeypatch.setattr(bot, 'DRY_RUN', False)
    monkeypatch.setattr(bot, 'ENABLE_LIVE_TRADING', True)
    db = Mock()
    db.query.return_value.filter.return_value.first.return_value = SimpleNamespace(
        is_active=True, dry_run=user_dry_run, enable_live_trading=True,
    )
    account = SimpleNamespace(dry_run=account_dry_run, user_id=1)
    assert bot.is_account_live_trading_enabled(db, account) is expected


def test_manual_market_order_is_simulated_for_live_user(monkeypatch):
    from app.routers import binance

    monkeypatch.setattr(binance, 'get_api_key_owned_by_user', Mock())
    monkeypatch.setattr(binance, 'get_futures_price', lambda _: 100.)
    decrypt = Mock()
    log = Mock()
    monkeypatch.setattr(binance, 'get_decrypted_api_keys', decrypt)
    monkeypatch.setattr(binance, 'create_trade_log', log)
    body = SimpleNamespace(api_key_id=1, symbol='BTCUSDT', side='BUY', quantity=1., reduce_only=False)
    user = SimpleNamespace(id=1, dry_run=False, enable_live_trading=True)
    assert binance.futures_market_order(body, Mock(), user)['dry_run']
    assert log.call_args.kwargs['status_value'] == 'DRY_RUN'
    decrypt.assert_not_called()


def test_dry_run_blocks_signed_writes(monkeypatch):
    post = Mock()
    monkeypatch.setattr(exchange.requests, 'post', post)
    with pytest.raises(exchange.HTTPException) as error:
        exchange.binance_signed_request('POST', exchange.BINANCE_FUTURES_BASE_URL, '/fapi/v1/order', 'k', 's')
    assert error.value.status_code == 403
    post.assert_not_called()


def test_failed_take_profit_closes_then_cancels_only_own_stop(monkeypatch):
    calls = []
    monkeypatch.setattr(bot, 'is_account_live_trading_enabled', lambda *_: True)
    monkeypatch.setattr(bot, 'get_futures_symbol_config', lambda *_: [{'symbol': 'BTCUSDT', 'leverage': 2}])
    monkeypatch.setattr(bot, 'futures_market_order_raw', lambda **_: {'executedQty': '1', 'avgPrice': '100'})
    monkeypatch.setattr(bot, 'normalize_price', lambda _, p: p)
    monkeypatch.setattr(bot, 'futures_stop_market_order_raw', lambda **_: {'algoId': 1})
    monkeypatch.setattr(bot, 'futures_take_profit_market_order_raw', Mock(side_effect=exchange.OrderExecutionUnknown()))
    monkeypatch.setattr(bot, 'emergency_close_position', lambda **_: calls.append('close') or {'executed': True})
    monkeypatch.setattr(bot, 'get_futures_open_algo_orders_raw', lambda *_: [
        {'algoId': 1, 'clientAlgoId': 'root_stop'}, {'algoId': 2, 'clientAlgoId': 'other_stop'},
    ])
    monkeypatch.setattr(bot, 'cancel_futures_algo_order_raw', lambda *args: calls.append(args[-1]))
    monkeypatch.setattr(bot, 'create_error_log', Mock())
    account = SimpleNamespace(id=1, user_id=1, leverage=2, is_running=True)
    result = bot.place_market_with_bracket(Mock(), account, 'k', 's', 'BTCUSDT', 'BUY', 'SELL', 1, 99., 108.5, 100., 'root')
    assert calls == ['close', 1]
    assert not account.is_running
    assert result['status'] == 'BRACKET_FAILED_EMERGENCY_HANDLED'


def test_restarted_bot_refuses_to_adopt_untracked_position():
    state = SimpleNamespace(runtime_state={})
    _, result = management.manage_position(None, SimpleNamespace(), state, 'k', 's', {'positionAmt': '1'}, True)
    assert result['message'] == 'UNMANAGED_POSITION'


def test_concurrent_tick_skips_before_order_path(monkeypatch):
    from app.db.database import engine

    connection = Mock()
    connection.execute.return_value.scalar.return_value = False
    context = Mock()
    context.__enter__ = Mock(return_value=connection)
    context.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(engine, 'connect', lambda: context)
    tick = Mock()
    monkeypatch.setattr(bot, '_execute_bot_tick', tick)
    result = bot.execute_bot_tick(Mock(), SimpleNamespace(api_key_id=1))
    assert result['message'] == 'ACCOUNT_TICK_IN_PROGRESS'
    tick.assert_not_called()


def test_wrong_futures_host_blocks_signed_write(monkeypatch):
    monkeypatch.setattr(exchange, 'DRY_RUN', False)
    monkeypatch.setattr(exchange, 'ENABLE_LIVE_TRADING', True)
    post = Mock()
    monkeypatch.setattr(exchange.requests, 'post', post)
    with pytest.raises(exchange.HTTPException) as error:
        exchange.binance_signed_request('POST', 'https://different.invalid', '/fapi/v1/order', 'k', 's')
    assert error.value.status_code == 403
    post.assert_not_called()
