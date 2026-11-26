from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest
import requests

from app.strategy import strategy_v57 as strategy
from app.services import binance_service as exchange


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


def test_override_is_restored_after_failure():
    original = strategy.B_RISK_NORMAL
    with pytest.raises(RuntimeError):
        with strategy.V57ConfigOverride({'B_RISK_NORMAL': .001}):
            raise RuntimeError()
    assert strategy.B_RISK_NORMAL == original


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
