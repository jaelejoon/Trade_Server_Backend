import uuid

from fastapi import HTTPException

from app.core.config import AUTO_CLOSE_ON_BRACKET_FAILURE, DRY_RUN, ENABLE_LIVE_TRADING
from app.core.security import now_utc
from app.models.trading_account import TradingAccount
from app.services.account_service import get_api_key_owned_by_user, get_or_create_bot_state
from app.services.binance_service import (
    calc_open_position_value_from_binance_position,
    cancel_all_futures_open_orders_raw,
    futures_market_order_raw,
    futures_set_leverage_raw,
    futures_stop_market_order_raw,
    futures_take_profit_market_order_raw,
    get_active_position,
    get_decrypted_api_keys,
    get_futures_open_orders_raw,
    get_futures_price,
    get_usdt_available_balance,
    is_min_notional_ok,
    normalize_price,
    normalize_quantity,
)
from app.services.error_log_service import create_error_log
from app.services.strategy_signal_service import get_v57_signal_from_db_candles
from app.services.trade_log_service import create_trade_log
from app.strategy.strategy_v57 import MAX_STOP_PCT, MAX_TOTAL_POSITION_RATIO, MAX_TOTAL_RISK_RATIO, MIN_STOP_PCT, SLIPPAGE


def _order_id(response):
    if isinstance(response, dict):
        value = response.get('orderId') or response.get('clientOrderId')
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


def reconcile_position_and_open_orders(api_key: str, secret_key: str, symbol: str):
    active_position = get_active_position(api_key=api_key, secret_key=secret_key, symbol=symbol)

    if DRY_RUN or not ENABLE_LIVE_TRADING:
        return {'active_position': active_position, 'open_orders': [], 'cleanup_executed': False, 'cleanup_result': None, 'dry_run': True}

    open_orders = get_futures_open_orders_raw(api_key=api_key, secret_key=secret_key, symbol=symbol)

    if active_position is None and open_orders:
        cleanup_result = cancel_all_futures_open_orders_raw(api_key=api_key, secret_key=secret_key, symbol=symbol)
        return {'active_position': None, 'open_orders': open_orders, 'cleanup_executed': True, 'cleanup_result': cleanup_result, 'dry_run': False}

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
        expected_entry_price = price * (1 + SLIPPAGE)
        stop_price = expected_entry_price - atr * stop_atr_mult
        risk_per_unit = expected_entry_price - stop_price
        take_profit_price = expected_entry_price + risk_per_unit * take_profit_r
    elif signal.get('side') == 'SHORT':
        expected_entry_price = price * (1 - SLIPPAGE)
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
    if not (MIN_STOP_PCT <= stop_pct <= MAX_STOP_PCT):
        return {'skip': True, 'reason': 'STOP_PCT_OUT_OF_RANGE', 'stop_pct': stop_pct, 'min_stop_pct': MIN_STOP_PCT, 'max_stop_pct': MAX_STOP_PCT}

    max_total_position_value = balance_reference * MAX_TOTAL_POSITION_RATIO
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
    max_total_risk_amount = balance_reference * MAX_TOTAL_RISK_RATIO

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


def place_market_with_bracket(db, account: TradingAccount, api_key: str, secret_key: str, symbol: str, entry_side: str, exit_side: str, quantity: float, stop_price: float, take_profit_price: float):
    client_order_root = f'v57_{account.id}_{uuid.uuid4().hex[:16]}'
    entry_response = None
    stop_response = None
    take_profit_response = None
    emergency_response = None

    if DRY_RUN or not ENABLE_LIVE_TRADING:
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

        stop_response = futures_stop_market_order_raw(api_key=api_key, secret_key=secret_key, symbol=symbol, side=exit_side, stop_price=stop_price, new_client_order_id=f'{client_order_root}_stop')
        take_profit_response = futures_take_profit_market_order_raw(api_key=api_key, secret_key=secret_key, symbol=symbol, side=exit_side, stop_price=take_profit_price, new_client_order_id=f'{client_order_root}_tp')

        return {'entry_response': entry_response, 'stop_response': stop_response, 'take_profit_response': take_profit_response, 'emergency_response': None, 'client_order_id': client_order_root, 'status': 'SUBMITTED', 'error_message': None, 'executed_quantity': executed_quantity}

    except Exception as exc:
        error_message = str(exc)
        if AUTO_CLOSE_ON_BRACKET_FAILURE and entry_response is not None:
            try:
                cancel_all_futures_open_orders_raw(api_key=api_key, secret_key=secret_key, symbol=symbol)
                executed_quantity = _executed_qty(entry_response, quantity)
                emergency_response = emergency_close_position(api_key=api_key, secret_key=secret_key, symbol=symbol, entry_side=entry_side, quantity=executed_quantity)
            except Exception as close_exc:
                emergency_response = {'executed': False, 'error': str(close_exc)}

        create_error_log(db=db, user_id=account.user_id, trading_account_id=account.id, source='place_market_with_bracket', message=error_message, raw_data=str({'entry': entry_response, 'stop': stop_response, 'take_profit': take_profit_response, 'emergency': emergency_response}))

        return {'entry_response': entry_response, 'stop_response': stop_response, 'take_profit_response': take_profit_response, 'emergency_response': emergency_response, 'client_order_id': client_order_root, 'status': 'BRACKET_FAILED_EMERGENCY_HANDLED' if emergency_response else 'BRACKET_FAILED', 'error_message': error_message}


def execute_bot_tick(db, account: TradingAccount):
    api_key_item = get_api_key_owned_by_user(db=db, user_id=account.user_id, api_key_id=account.api_key_id)
    api_key, secret_key = get_decrypted_api_keys(api_key_item)
    symbol = account.symbol.upper()

    try:
        reconcile_result = reconcile_position_and_open_orders(api_key=api_key, secret_key=secret_key, symbol=symbol)
    except Exception as exc:
        create_error_log(db=db, user_id=account.user_id, trading_account_id=account.id, source='execute_bot_tick.reconcile', message=str(exc))
        raise

    active_position = reconcile_result['active_position']
    signal_response = get_v57_signal_from_db_candles(db=db, user_id=account.user_id, symbol=symbol, sync_recent=True)

    if signal_response.get('status') != 'signal':
        signal_response['reconcile_result'] = reconcile_result
        return signal_response

    signal = signal_response['signal']
    state = get_or_create_bot_state(db, account.id)
    signal_time = signal.get('signal_time')
    strategy = signal.get('strategy')

    if state.last_signal_time == signal_time and state.last_strategy == strategy:
        return {'status': 'skipped', 'message': 'DUPLICATE_SIGNAL', 'symbol': symbol, 'signal': signal, 'reconcile_result': reconcile_result}

    if active_position:
        return {'status': 'skipped', 'message': 'POSITION_ALREADY_EXISTS', 'symbol': symbol, 'position': active_position, 'signal': signal, 'reconcile_result': reconcile_result}

    price = get_futures_price(symbol)
    available_usdt = get_usdt_available_balance(api_key, secret_key)
    current_position_value = calc_open_position_value_from_binance_position(active_position)

    order_plan = calculate_v57_order_plan(symbol=symbol, available_usdt=available_usdt, price=price, balance_reference=available_usdt, current_position_value=current_position_value, signal=signal)

    if order_plan is None:
        return {'status': 'skipped', 'message': 'ORDER_PLAN_NONE', 'symbol': symbol, 'signal': signal, 'reconcile_result': reconcile_result}

    if order_plan.get('skip'):
        return {'status': 'skipped', 'message': order_plan.get('reason'), 'symbol': symbol, 'signal': signal, 'order_plan': order_plan, 'reconcile_result': reconcile_result}

    entry_side = signal.get('binance_side')
    exit_side = 'SELL' if entry_side == 'BUY' else 'BUY'
    quantity = order_plan['quantity']
    stop_price = order_plan['stop_price']
    take_profit_price = order_plan['take_profit_price']

    placement = place_market_with_bracket(db=db, account=account, api_key=api_key, secret_key=secret_key, symbol=symbol, entry_side=entry_side, exit_side=exit_side, quantity=quantity, stop_price=stop_price, take_profit_price=take_profit_price)

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
        dry_run=DRY_RUN,
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
        raw_response=str({'entry': entry_response, 'stop': stop_response, 'take_profit': take_profit_response, 'emergency': emergency_response, 'order_plan': order_plan, 'reconcile_result': reconcile_result}),
    )

    if placement.get('status') in ('SUBMITTED', 'DRY_RUN'):
        state.last_signal_time = signal_time
        state.last_strategy = strategy
        state.updated_at = now_utc()
        db.commit()
        return {'status': 'order_created', 'dry_run': DRY_RUN, 'symbol': symbol, 'signal': signal, 'order_plan': order_plan, 'placement': placement, 'reconcile_result': reconcile_result}

    return {'status': 'order_failed', 'dry_run': DRY_RUN, 'symbol': symbol, 'signal': signal, 'order_plan': order_plan, 'placement': placement, 'reconcile_result': reconcile_result}
