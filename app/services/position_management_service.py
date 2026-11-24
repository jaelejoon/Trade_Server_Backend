from copy import deepcopy
import uuid

import pandas as pd

from app.core.security import now_utc
from app.services.binance_service import (
    cancel_futures_algo_order_raw,
    futures_market_order_raw,
    futures_stop_market_order_raw,
    get_futures_open_algo_orders_raw,
    get_futures_price,
    normalize_price,
)
from app.services.candle_service import CandleService
from app.strategy import strategy_v57 as engine


def make_position(signal, plan, placement, config, dry_run, entered_at):
    entry = placement.get('entry_response') or {}
    price = float(entry.get('avgPrice') or plan['expected_entry_price'])
    stop = placement.get('stop_price', plan['stop_price'])
    return {
        'side': signal['side'],
        'entry_price': price,
        'initial_risk': abs(price - stop),
        'stop_price': stop,
        'take_profit_price': placement.get('take_profit_price', plan['take_profit_price']),
        'qty': float(entry.get('executedQty') or plan['quantity']),
        'max_hold_bars': signal['max_hold_bars'],
        'bars_held': 0,
        'max_favorable_r': 0,
        'max_adverse_r': 0,
        'trailing_stop_activated': False,
        'entry_time': pd.Timestamp(entered_at).isoformat(),
        'last_bar': None,
        'dry_run': dry_run,
        'config': config,
        'stop_algo_id': (placement.get('stop_response') or {}).get('algoId'),
        'tp_algo_id': (placement.get('take_profit_response') or {}).get('algoId'),
        'liquidation_price': engine.calc_liquidation_price(price, signal['side']) if engine.ENABLE_LIQUIDATION_CHECK and engine.LIQUIDATION_LEVERAGE > 1 else None,
    }


def update_equity_guard(runtime, equity, now):
    now = pd.Timestamp(now)
    guard = runtime.setdefault('guard', {})
    for period, key in [('day', now.strftime('%Y-%m-%d')), ('month', now.strftime('%Y-%m'))]:
        if guard.get(period) != key:
            guard[period] = key
            guard[period + '_start'] = equity
    guard['peak'] = max(guard.get('peak', equity), equity)
    return engine.should_block_new_entry(equity, guard['peak'], guard['day_start'], guard['month_start'])


def _cancel_owned_protection(api_key, secret_key, symbol, position, account_id):
    owned = {str(position.get('stop_algo_id')), str(position.get('tp_algo_id'))}
    for order in get_futures_open_algo_orders_raw(api_key, secret_key, symbol):
        client_id = order.get('clientAlgoId', '')
        if str(order.get('algoId')) in owned or client_id.startswith((f'v57_{account_id}_', f'trail_{account_id}_')):
            cancel_futures_algo_order_raw(api_key, secret_key, order['algoId'])


def manage_position(db, account, state, api_key, secret_key, active_position, live, now=None):
    """수익 추적/EMA/시간 청산은 신규 신호 유무와 관계없이 매 tick 실행한다."""
    now = pd.Timestamp(now_utc() if now is None else now).tz_localize(None)
    runtime = deepcopy(state.runtime_state or {})
    position = runtime.get('position')
    if runtime.get('halted'):
        return runtime, {'status': 'blocked', 'message': runtime['halted']}
    if position and position['dry_run'] == live:
        return runtime, {'status': 'blocked', 'message': 'POSITION_MODE_CHANGED'}
    if not position:
        if active_position:
            return runtime, {'status': 'blocked', 'message': 'UNMANAGED_POSITION'}
        return runtime, None

    if live and active_position is None:
        _cancel_owned_protection(api_key, secret_key, account.symbol, position, account.id)
        runtime.pop('position')
        runtime['cooldown_until'] = (now + pd.Timedelta(minutes=15 * engine.COOLDOWN_BARS)).isoformat()
        return runtime, {'status': 'closed', 'message': 'EXCHANGE_POSITION_CLOSED'}
    if live:
        expected = position['qty'] * (1 if position['side'] == 'LONG' else -1)
        if abs(float(active_position['positionAmt']) - expected) > max(abs(expected) * 1e-6, 1e-9):
            runtime['halted'] = 'POSITION_CHANGED_OUTSIDE_BOT'
            return runtime, {'status': 'blocked', 'message': runtime['halted']}
        open_orders = get_futures_open_algo_orders_raw(api_key, secret_key, account.symbol)
        ids = {str(o.get('algoId')) for o in open_orders}
        if str(position.get('stop_algo_id')) not in ids or str(position.get('tp_algo_id')) not in ids:
            # 보호 주문이 사라졌으면 노출을 줄인다. 수량은 실제 거래소 값으로 확인한다.
            return _close_position(runtime, account, api_key, secret_key, position, live, now, 'PROTECTION_MISSING', float(active_position['markPrice']))

    raw = CandleService.load_recent_1m_dataframe(db, account.user_id, account.symbol, limit=2000)
    if live and now >= pd.Timestamp(position['entry_time']).tz_localize(None) + pd.Timedelta(minutes=15 * position['max_hold_bars']):
        return _close_position(runtime, account, api_key, secret_key, position, live, now, 'TIME_EXIT', get_futures_price(account.symbol))
    if raw.empty:
        return runtime, {'status': 'blocked', 'message': 'NO_MANAGEMENT_CANDLES'}
    raw['timestamp'] = pd.to_datetime(raw['timestamp'], utc=True).dt.tz_localize(None)
    raw = raw.loc[raw['timestamp'] < now.floor('min')].set_index('timestamp').sort_index()
    bars = engine.add_common_indicators(engine.resample_ohlcv(raw, '15min'), prefix='m15_')
    entered = pd.Timestamp(position['entry_time']).tz_localize(None)
    first_full_bar = entered.ceil('15min')
    first_bar = entered.floor('15min')
    bars = bars.loc[bars.index >= first_bar]
    if first_bar in bars.index and entered != first_bar:
        # 최초 부분 봉에서는 실제 진입 이전의 고저가를 사용하지 않는다.
        partial = raw.loc[(raw.index >= entered.ceil('min')) & (raw.index < first_full_bar)]
        if partial.empty:
            bars = bars.drop(first_bar)
        else:
            bars.loc[first_bar, ['open', 'high', 'low', 'close']] = [partial.iloc[0]['open'], partial['high'].max(), partial['low'].min(), partial.iloc[-1]['close']]
    if position.get('last_bar'):
        bars = bars.loc[bars.index > pd.Timestamp(position['last_bar'])]

    old_stop = position['stop_price']
    with engine.V57ConfigOverride(position['config']):
        for time, row in bars.iterrows():
            reason, price = engine.advance_position(position, row, exchange_managed=live)
            position['last_bar'] = time.isoformat()
            if reason:
                return _close_position(runtime, account, api_key, secret_key, position, live, now, reason, price)
        if now >= entered + pd.Timedelta(minutes=15 * position['max_hold_bars']):
            price = float(raw.iloc[-1]['close'])
            return _close_position(runtime, account, api_key, secret_key, position, live, now, 'TIME_EXIT', price)

    if live and position['stop_price'] != old_stop:
        new_price = normalize_price(account.symbol, position['stop_price'])
        if new_price != normalize_price(account.symbol, old_stop):
            # 먼저 새 보호 주문을 확보한 뒤 이전 주문을 취소한다.
            side = 'SELL' if position['side'] == 'LONG' else 'BUY'
            response = futures_stop_market_order_raw(api_key, secret_key, account.symbol, side, new_price, f'trail_{account.id}_{uuid.uuid4().hex[:16]}')
            if position.get('stop_algo_id'):
                cancel_futures_algo_order_raw(api_key, secret_key, position['stop_algo_id'])
            position['stop_algo_id'] = response['algoId']
        position['stop_price'] = new_price
    runtime['position'] = position
    return runtime, {'status': 'holding', 'position': position}


def _close_position(runtime, account, api_key, secret_key, position, live, now, reason, price):
    if live:
        side = 'SELL' if position['side'] == 'LONG' else 'BUY'
        response = futures_market_order_raw(api_key, secret_key, account.symbol, side, position['qty'], reduce_only=True, new_client_order_id=f'exit_{uuid.uuid4().hex[:24]}')
        _cancel_owned_protection(api_key, secret_key, account.symbol, position, account.id)
        price = float(response['avgPrice'])
    else:
        config = position['config']
        price *= 1 - config['SLIPPAGE'] if position['side'] == 'LONG' else 1 + config['SLIPPAGE']
        sign = 1 if position['side'] == 'LONG' else -1
        pnl = sign * position['qty'] * (price - position['entry_price'])
        runtime['paper_balance'] += pnl - position['qty'] * price * config['FEE_RATE']
    runtime.pop('position', None)
    runtime['cooldown_until'] = (now + pd.Timedelta(minutes=15 * engine.COOLDOWN_BARS)).isoformat()
    runtime['last_exit'] = {'reason': reason, 'price': price, 'time': now.isoformat()}
    return runtime, {'status': 'closed', 'message': reason, 'price': price}
