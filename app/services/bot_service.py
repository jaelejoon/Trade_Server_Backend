import uuid
from copy import deepcopy

import pandas as pd
from sqlalchemy import text
from datetime import timedelta

from fastapi import HTTPException

from app.core.config import (
    AUTO_CLOSE_ON_BRACKET_FAILURE,
    DRY_RUN,
    ENABLE_LIVE_TRADING,
    STRATEGY_CALCULATION_GRACE_SECONDS,
    STRATEGY_CALCULATION_MODE,
)
from app.core.security import now_utc, to_utc_isoformat
from app.models.trading_account import TradingAccount
from app.models.user import User
from app.services.account_service import get_api_key_owned_by_user, get_or_create_bot_state
from app.services.binance_service import (
    calc_open_position_value_from_binance_position,
    get_futures_open_algo_orders_raw,
    get_usdt_equity,
    futures_market_order_raw,
    futures_set_leverage_raw,
    futures_stop_market_order_raw,
    futures_take_profit_market_order_raw,
    get_active_position,
    get_decrypted_api_keys,
    get_futures_price,
    get_usdt_available_balance,
    is_min_notional_ok,
    normalize_price,
    normalize_quantity,
)
from app.services.error_log_service import create_error_log
from app.services.latest_signal_service import (
    get_latest_signal_state_by_account,
    serialize_latest_signal_state,
    upsert_latest_signal_state,
)
from app.services.strategy_signal_service import get_v57_signal_from_db_candles
from app.services.binance_candle_sync import BinanceCandleSyncService
from app.services.trade_log_service import create_trade_log
from app.strategy import strategy_v57 as strategy_engine
from app.services.trading_strategy_service import get_effective_strategy_for_user
from app.services.position_management_service import manage_position, update_equity_guard, make_position



def is_account_live_trading_enabled(db, account: TradingAccount) -> bool:
    if DRY_RUN or not ENABLE_LIVE_TRADING or account.dry_run:
        return False

    user = db.query(User).filter(User.id == account.user_id).first()
    if not user or not user.is_active:
        return False

    return bool(user.enable_live_trading)

def _order_id(response):
    if isinstance(response, dict):
        value = response.get('algoId') or response.get('orderId') or response.get('clientAlgoId') or response.get('clientOrderId')
        return str(value) if value is not None else None
    return None


def _executed_qty(response, fallback_qty: float):
    if isinstance(response, dict):
        for key in ('executedQty', 'cumQty', 'origQty'):
            try:
                value = float(response.get(key, 0))
                if value > 0:
                    return value
            except Exception:
                pass
    return fallback_qty


def _avg_fill_price(response, fallback_price: float):
    if isinstance(response, dict):
        try:
            avg_price = float(response.get('avgPrice', 0))
            if avg_price > 0:
                return avg_price
        except Exception:
            pass
    return fallback_price


def reconcile_position_and_open_orders(api_key: str, secret_key: str, symbol: str, account_dry_run: bool = True, live_trading_enabled: bool = False):
    active_position = get_active_position(api_key=api_key, secret_key=secret_key, symbol=symbol)

    if account_dry_run or not live_trading_enabled:
        return {'active_position': active_position, 'open_orders': [], 'cleanup_executed': False, 'cleanup_result': None, 'dry_run': True}

    open_orders = get_futures_open_algo_orders_raw(api_key, secret_key, symbol)
    return {'active_position': active_position, 'open_orders': open_orders, 'cleanup_executed': False, 'cleanup_result': None, 'dry_run': False}


def calculate_v57_order_plan(symbol: str, available_usdt: float, price: float, balance_reference: float, current_position_value: float, signal):
    if not signal.get('has_signal'):
        return None

    atr = signal.get('atr')
    if atr is None or atr <= 0:
        return None

    risk_per_trade = signal.get('risk_per_trade')
    max_position_ratio = signal.get('max_position_ratio')
    stop_atr_mult = signal.get('stop_atr_mult')
    take_profit_r = signal.get('take_profit_r')

    if risk_per_trade is None or max_position_ratio is None:
        return None

    if signal.get('side') == 'LONG':
        expected_entry_price = price * (1 + strategy_engine.SLIPPAGE)
        stop_price = expected_entry_price - atr * stop_atr_mult
        risk_per_unit = expected_entry_price - stop_price
        take_profit_price = expected_entry_price + risk_per_unit * take_profit_r
    elif signal.get('side') == 'SHORT':
        expected_entry_price = price * (1 - strategy_engine.SLIPPAGE)
        stop_price = expected_entry_price + atr * stop_atr_mult
        risk_per_unit = stop_price - expected_entry_price
        take_profit_price = expected_entry_price - risk_per_unit * take_profit_r
    else:
        return None

    if risk_per_unit <= 0:
        return None

    stop_price = normalize_price(symbol, stop_price)
    take_profit_price = normalize_price(symbol, take_profit_price)

    if stop_price <= 0 or take_profit_price <= 0:
        return {'skip': True, 'reason': 'PRICE_NORMALIZE_FAILED', 'stop_price': stop_price, 'take_profit_price': take_profit_price}

    if signal.get('side') == 'LONG':
        risk_per_unit = expected_entry_price - stop_price
    else:
        risk_per_unit = stop_price - expected_entry_price

    if risk_per_unit <= 0:
        return {'skip': True, 'reason': 'RISK_PER_UNIT_INVALID_AFTER_PRICE_NORMALIZE', 'risk_per_unit': risk_per_unit}

    stop_pct = risk_per_unit / expected_entry_price
    if not (strategy_engine.MIN_STOP_PCT <= stop_pct <= strategy_engine.MAX_STOP_PCT):
        return {'skip': True, 'reason': 'STOP_PCT_OUT_OF_RANGE', 'stop_pct': stop_pct, 'min_stop_pct': strategy_engine.MIN_STOP_PCT, 'max_stop_pct': strategy_engine.MAX_STOP_PCT}

    max_total_position_value = balance_reference * strategy_engine.MAX_TOTAL_POSITION_RATIO
    remaining_position_capacity = max_total_position_value - current_position_value
    if remaining_position_capacity <= 0:
        return {'skip': True, 'reason': 'NO_POSITION_CAPACITY', 'remaining_position_capacity': remaining_position_capacity}

    account_risk = balance_reference * risk_per_trade
    max_position_value = min(balance_reference * max_position_ratio, remaining_position_capacity, available_usdt * 0.95)
    quantity_by_risk = account_risk / risk_per_unit
    quantity_by_position_value = max_position_value / expected_entry_price
    quantity = normalize_quantity(symbol, min(quantity_by_risk, quantity_by_position_value))

    if quantity <= 0:
        return {'skip': True, 'reason': 'QTY_ZERO_AFTER_SYMBOL_FILTER', 'quantity': quantity}

    if not is_min_notional_ok(symbol, quantity, expected_entry_price):
        return {'skip': True, 'reason': 'MIN_NOTIONAL_NOT_MET', 'quantity': quantity, 'expected_entry_price': expected_entry_price, 'notional': quantity * expected_entry_price}

    initial_risk_amount = risk_per_unit * quantity
    position_value = expected_entry_price * quantity
    max_total_risk_amount = balance_reference * strategy_engine.MAX_TOTAL_RISK_RATIO

    if initial_risk_amount > max_total_risk_amount:
        return {'skip': True, 'reason': 'MAX_TOTAL_RISK_EXCEEDED', 'initial_risk_amount': initial_risk_amount, 'max_total_risk_amount': max_total_risk_amount}

    return {'skip': False, 'reason': 'OK', 'quantity': quantity, 'expected_entry_price': expected_entry_price, 'stop_price': stop_price, 'take_profit_price': take_profit_price, 'risk_per_unit': risk_per_unit, 'stop_pct': stop_pct, 'position_value': position_value, 'initial_risk_amount': initial_risk_amount, 'account_risk': account_risk, 'max_position_value': max_position_value}


def emergency_close_position(api_key: str, secret_key: str, symbol: str, entry_side: str, quantity: float):
    close_side = 'SELL' if entry_side == 'BUY' else 'BUY'
    safe_qty = normalize_quantity(symbol, quantity)
    if safe_qty <= 0:
        return {'executed': False, 'reason': 'QTY_ZERO'}
    response = futures_market_order_raw(api_key=api_key, secret_key=secret_key, symbol=symbol, side=close_side, quantity=safe_qty, reduce_only=True, new_client_order_id=f'emergency_{uuid.uuid4().hex[:20]}')
    return {'executed': True, 'response': response}


def place_market_with_bracket(db, account: TradingAccount, api_key: str, secret_key: str, symbol: str, entry_side: str, exit_side: str, quantity: float, stop_price: float, take_profit_price: float, expected_entry_price: float, client_order_root: str | None = None):
    client_order_root = client_order_root or f'v57_{account.id}_{uuid.uuid4().hex[:12]}'
    entry_response = None
    stop_response = None
    take_profit_response = None
    emergency_response = None

    live_trading_enabled = is_account_live_trading_enabled(db, account)

    if not live_trading_enabled:
        return {
            'entry_response': {'dry_run': True, 'type': 'MARKET', 'side': entry_side, 'symbol': symbol, 'quantity': quantity, 'clientOrderId': f'{client_order_root}_entry'},
            'stop_response': {'dry_run': True, 'type': 'STOP_MARKET', 'side': exit_side, 'stopPrice': stop_price, 'closePosition': True, 'clientOrderId': f'{client_order_root}_stop'},
            'take_profit_response': {'dry_run': True, 'type': 'TAKE_PROFIT_MARKET', 'side': exit_side, 'stopPrice': take_profit_price, 'closePosition': True, 'clientOrderId': f'{client_order_root}_tp'},
            'emergency_response': None,
            'client_order_id': client_order_root,
            'status': 'DRY_RUN',
            'error_message': None,
        }

    futures_set_leverage_raw(api_key=api_key, secret_key=secret_key, symbol=symbol, leverage=account.leverage)

    try:
        entry_response = futures_market_order_raw(api_key=api_key, secret_key=secret_key, symbol=symbol, side=entry_side, quantity=quantity, reduce_only=False, new_client_order_id=f'{client_order_root}_entry')
        executed_quantity = _executed_qty(entry_response, quantity)
        shift = float(entry_response['avgPrice']) - expected_entry_price
        stop_price = normalize_price(symbol, stop_price + shift)
        take_profit_price = normalize_price(symbol, take_profit_price + shift)

        stop_response = futures_stop_market_order_raw(api_key=api_key, secret_key=secret_key, symbol=symbol, side=exit_side, stop_price=stop_price, new_client_order_id=f'{client_order_root}_stop')
        take_profit_response = futures_take_profit_market_order_raw(api_key=api_key, secret_key=secret_key, symbol=symbol, side=exit_side, stop_price=take_profit_price, new_client_order_id=f'{client_order_root}_tp')

        if not stop_response.get('algoId') or not take_profit_response.get('algoId'):
            raise RuntimeError('보호 주문 접수 번호를 확인할 수 없습니다.')

        return {'entry_response': entry_response, 'stop_response': stop_response, 'take_profit_response': take_profit_response, 'emergency_response': None, 'client_order_id': client_order_root, 'status': 'SUBMITTED', 'error_message': None, 'executed_quantity': executed_quantity, 'stop_price': stop_price, 'take_profit_price': take_profit_price}

    except Exception:
        error_message = '진입 또는 보호 주문 처리 실패'
        if AUTO_CLOSE_ON_BRACKET_FAILURE and entry_response is not None:
            try:
                executed_quantity = _executed_qty(entry_response, quantity)
                emergency_response = emergency_close_position(api_key=api_key, secret_key=secret_key, symbol=symbol, entry_side=entry_side, quantity=executed_quantity)
            except Exception:
                emergency_response = {'executed': False, 'error': '비상 종료 처리 실패'}

        account.is_running = False
        db.commit()
        create_error_log(db=db, user_id=account.user_id, trading_account_id=account.id, source='place_market_with_bracket', message=error_message)

        return {'entry_response': entry_response, 'stop_response': stop_response, 'take_profit_response': take_profit_response, 'emergency_response': emergency_response, 'client_order_id': client_order_root, 'status': 'BRACKET_FAILED_EMERGENCY_HANDLED' if emergency_response else 'BRACKET_FAILED', 'error_message': error_message}



def _latest_closed_1h_candle_time():
    now = now_utc().replace(tzinfo=None)
    current_hour = now.replace(minute=0, second=0, microsecond=0)
    seconds_after_hour = (
        now.minute * 60
        + now.second
        + (now.microsecond / 1_000_000)
    )

    if seconds_after_hour >= STRATEGY_CALCULATION_GRACE_SECONDS:
        return current_hour - timedelta(hours=1)

    return current_hour - timedelta(hours=2)


def _get_cached_signal_response(state):
    if state is None:
        return None

    cached = serialize_latest_signal_state(state)
    if not isinstance(cached, dict):
        return None

    signal_response = cached.get('signal_response')
    if isinstance(signal_response, dict):
        return signal_response

    return None


def _get_cached_closed_1h_candle_time(state):
    signal_response = _get_cached_signal_response(state)
    if not isinstance(signal_response, dict):
        return None

    context = signal_response.get('calculation_context')
    if not isinstance(context, dict):
        return None

    return context.get('closed_1h_candle_time')


def _should_calculate_strategy(state):
    if STRATEGY_CALCULATION_MODE not in {'closed_1h', 'closed_candle'}:
        return True, None, 'CALCULATION_MODE_ALWAYS'

    target_time = _latest_closed_1h_candle_time()
    target_key = to_utc_isoformat(target_time)

    if state is None:
        return True, target_time, 'NO_CACHED_SIGNAL'

    cached_signal_response = _get_cached_signal_response(state)
    if not isinstance(cached_signal_response, dict) or cached_signal_response.get('status') == 'error':
        return True, target_time, 'NO_CACHED_SIGNAL_RESPONSE'

    cached_key = _get_cached_closed_1h_candle_time(state)
    current_config = {key: getattr(strategy_engine, key) for key in strategy_engine.DEFAULT_V57_CONFIG}
    if cached_signal_response.get('strategy_config') != current_config:
        return True, target_time, 'STRATEGY_CONFIG_CHANGED'
    if cached_key != target_key:
        return True, target_time, 'NEW_CLOSED_1H_CANDLE'

    return False, target_time, 'CACHED_SIGNAL_IS_CURRENT'


def _attach_calculation_context(signal_response, target_time, calculation_reason):
    if not isinstance(signal_response, dict):
        return signal_response

    context = signal_response.get('calculation_context')
    if not isinstance(context, dict):
        context = {}

    context['mode'] = STRATEGY_CALCULATION_MODE
    context['reason'] = calculation_reason
    context['calculated_at'] = to_utc_isoformat(now_utc())

    if target_time is not None:
        context['closed_1h_candle_time'] = to_utc_isoformat(target_time)

    signal_response['calculation_context'] = context
    return signal_response

def _save_latest_signal_state_safe(db, account: TradingAccount, signal_response=None, tick_response=None, error_message=None):
    try:
        upsert_latest_signal_state(
            db=db,
            user_id=account.user_id,
            trading_account_id=account.id,
            symbol=account.symbol,
            signal_response=signal_response,
            tick_response=tick_response,
            error_message=error_message,
        )
    except Exception:
        db.rollback()
        create_error_log(
            db=db,
            user_id=account.user_id,
            trading_account_id=account.id,
            source='save_latest_signal_state',
            message='최근 신호 상태 저장 실패',
        )


def _execute_bot_tick(db, account: TradingAccount):
    api_key_item = get_api_key_owned_by_user(db=db, user_id=account.user_id, api_key_id=account.api_key_id)
    api_key, secret_key = get_decrypted_api_keys(api_key_item)
    symbol = account.symbol.upper()

    try:
        reconcile_result = reconcile_position_and_open_orders(
            api_key=api_key,
            secret_key=secret_key,
            symbol=symbol,
            account_dry_run=account.dry_run,
            live_trading_enabled=is_account_live_trading_enabled(db, account),
        )
    except Exception:
        create_error_log(db=db, user_id=account.user_id, trading_account_id=account.id, source='execute_bot_tick.reconcile', message='포지션 및 미체결 주문 조회 실패')
        raise

    active_position = reconcile_result['active_position']

    BinanceCandleSyncService.sync_recent_1m_candles(db, account.user_id, symbol, limit=1500)
    state = get_or_create_bot_state(db, account.id)
    live = is_account_live_trading_enabled(db, account)
    prior_position = deepcopy((state.runtime_state or {}).get('position'))
    runtime, management = manage_position(db, account, state, api_key, secret_key, active_position, live)
    state.runtime_state = runtime
    state.updated_at = now_utc()
    db.commit()
    if management and management['status'] == 'closed' and prior_position:
        create_trade_log(
            db=db, user_id=account.user_id, api_key_id=account.api_key_id,
            trading_account_id=account.id, symbol=symbol,
            side='SELL' if prior_position['side'] == 'LONG' else 'BUY',
            order_type='MANAGED_EXIT', qty=prior_position['qty'],
            price=management.get('price'), reduce_only=True,
            dry_run=prior_position['dry_run'], status_value=management['message'],
            strategy=state.last_strategy, signal_time=state.last_signal_time,
            stop_price=prior_position['stop_price'],
            take_profit_price=prior_position['take_profit_price'],
            raw_response=str(management),
        )
    if runtime.get('pending_entry'):
        return {'status': 'blocked', 'message': 'ENTRY_RECONCILIATION_REQUIRED'}
    if runtime.get('cooldown_until') and pd.Timestamp(now_utc()).tz_localize(None) < pd.Timestamp(runtime['cooldown_until']):
        return {'status': 'skipped', 'message': 'COOLDOWN'}
    equity = get_usdt_equity(api_key, secret_key) if live else runtime.get('paper_balance')
    if equity is None:
        equity = get_usdt_available_balance(api_key, secret_key)
        runtime['paper_balance'] = equity
    if not live and runtime.get('position'):
        position = runtime['position']
        sign = 1 if position['side'] == 'LONG' else -1
        equity += sign * position['qty'] * (get_futures_price(symbol) - position['entry_price'])
    blocked, reason = update_equity_guard(runtime, equity, now_utc())
    state.runtime_state = deepcopy(runtime)
    db.commit()
    if management and management['status'] in {'blocked', 'closed', 'holding'}:
        result = {'status': management['status'], 'message': management.get('message', 'POSITION_ALREADY_EXISTS'), 'management': management, 'dry_run': not live}
        _save_latest_signal_state_safe(db, account, tick_response=result)
        return result
    if equity <= 0:
        return {'status': 'skipped', 'message': 'NO_AVAILABLE_EQUITY'}
    if blocked:
        return {'status': 'skipped', 'message': reason}

    latest_state = get_latest_signal_state_by_account(
        db=db,
        user_id=account.user_id,
        trading_account_id=account.id,
    )
    should_calculate, target_time, calculation_reason = _should_calculate_strategy(latest_state)

    if should_calculate:
        signal_response = get_v57_signal_from_db_candles(
            db=db,
            user_id=account.user_id,
            symbol=symbol,
            sync_recent=False,
            trading_account_id=account.id,
        )
        signal_response = _attach_calculation_context(
            signal_response=signal_response,
            target_time=target_time,
            calculation_reason=calculation_reason,
        )
        _save_latest_signal_state_safe(db, account, signal_response=signal_response)
    else:
        signal_response = _get_cached_signal_response(latest_state)
        signal_response = _attach_calculation_context(
            signal_response=signal_response,
            target_time=target_time,
            calculation_reason=calculation_reason,
        )

    if signal_response.get('status') != 'signal':
        signal_response['reconcile_result'] = reconcile_result
        signal_response['strategy_calculation_skipped'] = not should_calculate
        signal_response['strategy_calculation_reason'] = calculation_reason
        _save_latest_signal_state_safe(db, account, tick_response=signal_response)
        return signal_response

    signal = signal_response['signal']
    signal_time = signal.get('signal_time')
    strategy = signal.get('strategy')
    signal_at = pd.Timestamp(signal_time).tz_localize(None)
    age = pd.Timestamp(now_utc()).tz_localize(None) - signal_at
    if age < pd.Timedelta(0) or age > pd.Timedelta(minutes=5):
        return {'status': 'skipped', 'message': 'SIGNAL_EXPIRED'}

    if state.last_signal_time == signal_time and state.last_strategy == strategy:
        result = {'status': 'skipped', 'message': 'DUPLICATE_SIGNAL', 'symbol': symbol, 'signal': signal, 'reconcile_result': reconcile_result, 'strategy_calculation_skipped': not should_calculate, 'strategy_calculation_reason': calculation_reason}
        _save_latest_signal_state_safe(db, account, tick_response=result)
        return result

    if active_position:
        result = {'status': 'skipped', 'message': 'POSITION_ALREADY_EXISTS', 'symbol': symbol, 'position': active_position, 'signal': signal, 'reconcile_result': reconcile_result, 'strategy_calculation_skipped': not should_calculate, 'strategy_calculation_reason': calculation_reason}
        _save_latest_signal_state_safe(db, account, tick_response=result)
        return result

    price = get_futures_price(symbol)
    available_usdt = get_usdt_available_balance(api_key, secret_key) if live else runtime['paper_balance']
    current_position_value = calc_open_position_value_from_binance_position(active_position)

    order_plan = calculate_v57_order_plan(symbol=symbol, available_usdt=available_usdt, price=price, balance_reference=equity, current_position_value=current_position_value, signal=signal)

    if order_plan is None:
        result = {'status': 'skipped', 'message': 'ORDER_PLAN_NONE', 'symbol': symbol, 'signal': signal, 'reconcile_result': reconcile_result, 'strategy_calculation_skipped': not should_calculate, 'strategy_calculation_reason': calculation_reason}
        _save_latest_signal_state_safe(db, account, tick_response=result)
        return result

    if order_plan.get('skip'):
        result = {'status': 'skipped', 'message': order_plan.get('reason'), 'symbol': symbol, 'signal': signal, 'order_plan': order_plan, 'reconcile_result': reconcile_result, 'strategy_calculation_skipped': not should_calculate, 'strategy_calculation_reason': calculation_reason}
        _save_latest_signal_state_safe(db, account, tick_response=result)
        return result

    entry_side = signal.get('binance_side')
    exit_side = 'SELL' if entry_side == 'BUY' else 'BUY'
    quantity = order_plan['quantity']
    stop_price = order_plan['stop_price']
    take_profit_price = order_plan['take_profit_price']

    client_order_root = f'v57_{account.id}_{uuid.uuid4().hex[:12]}'
    runtime['pending_entry'] = {'signal_time': signal_time, 'plan': order_plan, 'client_order_id': client_order_root}
    state.last_signal_time = signal_time
    state.last_strategy = strategy
    state.runtime_state = deepcopy(runtime)
    db.commit()

    placement = place_market_with_bracket(db=db, account=account, api_key=api_key, secret_key=secret_key, symbol=symbol, entry_side=entry_side, exit_side=exit_side, quantity=quantity, stop_price=stop_price, take_profit_price=take_profit_price, expected_entry_price=order_plan['expected_entry_price'], client_order_root=client_order_root)

    if placement.get('status') in ('SUBMITTED', 'DRY_RUN'):
        config = {key: deepcopy(getattr(strategy_engine, key)) for key in strategy_engine.DEFAULT_V57_CONFIG}
        runtime['position'] = make_position(signal, order_plan, placement, config, not live, now_utc())
        runtime.pop('pending_entry', None)
        if not live:
            runtime['paper_balance'] -= order_plan['position_value'] * strategy_engine.FEE_RATE
        stop_price = runtime['position']['stop_price']
        take_profit_price = runtime['position']['take_profit_price']
    else:
        runtime['halted'] = 'BRACKET_FAILED_RECONCILIATION_REQUIRED'
    state.runtime_state = deepcopy(runtime)
    db.commit()

    entry_response = placement.get('entry_response')
    stop_response = placement.get('stop_response')
    take_profit_response = placement.get('take_profit_response')
    emergency_response = placement.get('emergency_response')
    executed_qty = _executed_qty(entry_response, quantity) if entry_response else None
    avg_fill_price = _avg_fill_price(entry_response, price) if entry_response else None

    create_trade_log(
        db=db,
        user_id=account.user_id,
        api_key_id=account.api_key_id,
        trading_account_id=account.id,
        symbol=symbol,
        side=entry_side,
        order_type='MARKET_WITH_BRACKET',
        qty=quantity,
        executed_qty=executed_qty,
        price=price,
        avg_fill_price=avg_fill_price,
        reduce_only=False,
        dry_run=not is_account_live_trading_enabled(db, account),
        status_value=placement.get('status'),
        strategy=strategy,
        signal_time=signal_time,
        stop_price=stop_price,
        take_profit_price=take_profit_price,
        entry_order_id=_order_id(entry_response),
        stop_order_id=_order_id(stop_response),
        take_profit_order_id=_order_id(take_profit_response),
        client_order_id=placement.get('client_order_id'),
        emergency_close_executed=bool(emergency_response and emergency_response.get('executed')),
        emergency_close_order_id=_order_id(emergency_response.get('response')) if isinstance(emergency_response, dict) else None,
        error_message=placement.get('error_message'),
        raw_response=None if placement.get('error_message') else str({'entry': entry_response, 'stop': stop_response, 'take_profit': take_profit_response, 'emergency': emergency_response, 'order_plan': order_plan, 'reconcile_result': reconcile_result}),
    )

    if placement.get('status') in ('SUBMITTED', 'DRY_RUN'):
        state.last_signal_time = signal_time
        state.last_strategy = strategy
        state.updated_at = now_utc()
        db.commit()
        result = {'status': 'order_created', 'dry_run': not is_account_live_trading_enabled(db, account), 'symbol': symbol, 'signal': signal, 'order_plan': order_plan, 'placement': placement, 'reconcile_result': reconcile_result, 'strategy_calculation_skipped': not should_calculate, 'strategy_calculation_reason': calculation_reason}
        _save_latest_signal_state_safe(db, account, tick_response=result)
        return result

    result = {'status': 'order_failed', 'dry_run': not is_account_live_trading_enabled(db, account), 'symbol': symbol, 'signal': signal, 'order_plan': order_plan, 'placement': placement, 'reconcile_result': reconcile_result, 'strategy_calculation_skipped': not should_calculate, 'strategy_calculation_reason': calculation_reason}
    _save_latest_signal_state_safe(db, account, tick_response=result)
    return result


def execute_bot_tick(db, account: TradingAccount):
    # 같은 API 키의 tick과 수동 실행을 프로세스 간에도 직렬화한다.
    from app.db.database import engine as database_engine
    with database_engine.connect() as lock_connection:
        locked = lock_connection.execute(text("SELECT pg_try_advisory_lock(571124, :key)"), {'key': account.api_key_id}).scalar()
        if not locked:
            return {'status': 'skipped', 'message': 'ACCOUNT_TICK_IN_PROGRESS'}
        try:
            other = db.query(TradingAccount).filter(
                TradingAccount.api_key_id == account.api_key_id,
                TradingAccount.id != account.id,
                TradingAccount.is_running == True,
            ).first()
            if other:
                return {'status': 'blocked', 'message': 'API_KEY_ALREADY_USED_BY_RUNNING_BOT'}
            _, config, _ = get_effective_strategy_for_user(db, account.user_id, account.id)
            with strategy_engine.V57ConfigOverride(config):
                return _execute_bot_tick(db, account)
        except Exception:
            db.rollback()
            state = get_or_create_bot_state(db, account.id)
            runtime = deepcopy(state.runtime_state or {})
            if runtime.get('pending_entry') or runtime.get('position'):
                runtime['halted'] = 'ORDER_OR_POSITION_RECONCILIATION_REQUIRED'
                state.runtime_state = runtime
                account.is_running = False
                db.commit()
            raise
        finally:
            lock_connection.execute(text("SELECT pg_advisory_unlock(571124, :key)"), {'key': account.api_key_id})
