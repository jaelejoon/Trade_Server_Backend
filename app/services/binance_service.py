import hashlib
import hmac
import time
from decimal import Decimal, ROUND_DOWN
from functools import lru_cache
from typing import Literal, Optional
from urllib.parse import urlencode

import pandas as pd
import requests
from fastapi import HTTPException

from app.core.config import (
    BINANCE_FUTURES_BASE_URL,
    BINANCE_MAX_RETRIES,
    BINANCE_REQUEST_TIMEOUT_SECONDS,
    BINANCE_RETRY_BASE_SLEEP_SECONDS,
    BINANCE_SPOT_BASE_URL,
    SERVER_TIME_DRIFT_LIMIT_MS,
)
from app.core.security import decrypt_text
from app.models.binance_api_key import BinanceApiKey

RETRYABLE_BINANCE_CODES = {-1001, -1007, -1021}


def get_decrypted_api_keys(item: BinanceApiKey):
    return decrypt_text(item.api_key_encrypted), decrypt_text(item.secret_key_encrypted)


def _parse_response(response: requests.Response):
    try:
        return response.json()
    except Exception:
        return {'raw': response.text}


def _raise_binance_error(response: requests.Response, data):
    raise HTTPException(
        status_code=400,
        detail={
            'message': 'Binance API 요청 실패',
            'binance_status': response.status_code,
            'binance_response': data,
        },
    )


def _is_retryable_response(status_code: int, data) -> bool:
    if status_code >= 500:
        return True
    if isinstance(data, dict):
        try:
            return int(data.get('code', 0)) in RETRYABLE_BINANCE_CODES
        except Exception:
            return False
    return False


def binance_signed_request(
    method: Literal['GET', 'POST', 'DELETE'],
    base_url: str,
    path: str,
    api_key: str,
    secret_key: str,
    params: Optional[dict] = None,
):
    if params is None:
        params = {}

    last_error = None

    for attempt in range(BINANCE_MAX_RETRIES):
        request_params = dict(params)
        request_params['timestamp'] = int(time.time() * 1000)
        request_params['recvWindow'] = 5000

        query_string = urlencode(request_params, doseq=True)
        signature = hmac.new(
            secret_key.encode(),
            query_string.encode(),
            hashlib.sha256,
        ).hexdigest()

        url = f'{base_url}{path}?{query_string}&signature={signature}'
        headers = {'X-MBX-APIKEY': api_key}

        try:
            if method == 'GET':
                response = requests.get(url, headers=headers, timeout=BINANCE_REQUEST_TIMEOUT_SECONDS)
            elif method == 'POST':
                response = requests.post(url, headers=headers, timeout=BINANCE_REQUEST_TIMEOUT_SECONDS)
            elif method == 'DELETE':
                response = requests.delete(url, headers=headers, timeout=BINANCE_REQUEST_TIMEOUT_SECONDS)
            else:
                raise ValueError('지원하지 않는 HTTP method입니다.')
        except requests.RequestException as exc:
            last_error = exc
            if attempt < BINANCE_MAX_RETRIES - 1:
                time.sleep(BINANCE_RETRY_BASE_SLEEP_SECONDS * (2 ** attempt))
                continue
            raise HTTPException(status_code=503, detail=f'Binance 네트워크 요청 실패: {exc}')

        data = _parse_response(response)

        if response.status_code < 400:
            return data

        if _is_retryable_response(response.status_code, data) and attempt < BINANCE_MAX_RETRIES - 1:
            time.sleep(BINANCE_RETRY_BASE_SLEEP_SECONDS * (2 ** attempt))
            continue

        _raise_binance_error(response, data)

    raise HTTPException(status_code=503, detail=f'Binance 요청 재시도 실패: {last_error}')


def binance_public_get(base_url: str, path: str, params: Optional[dict] = None):
    if params is None:
        params = {}

    last_error = None

    for attempt in range(BINANCE_MAX_RETRIES):
        try:
            response = requests.get(
                f'{base_url}{path}',
                params=params,
                timeout=BINANCE_REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            last_error = exc
            if attempt < BINANCE_MAX_RETRIES - 1:
                time.sleep(BINANCE_RETRY_BASE_SLEEP_SECONDS * (2 ** attempt))
                continue
            raise HTTPException(status_code=503, detail=f'Binance Public API 네트워크 요청 실패: {exc}')

        data = _parse_response(response)
        if response.status_code < 400:
            return data

        if _is_retryable_response(response.status_code, data) and attempt < BINANCE_MAX_RETRIES - 1:
            time.sleep(BINANCE_RETRY_BASE_SLEEP_SECONDS * (2 ** attempt))
            continue

        raise HTTPException(
            status_code=400,
            detail={
                'message': 'Binance Public API 요청 실패',
                'binance_status': response.status_code,
                'binance_response': data,
            },
        )

    raise HTTPException(status_code=503, detail=f'Binance Public API 재시도 실패: {last_error}')


def get_binance_server_time_ms() -> int:
    data = binance_public_get(
        base_url=BINANCE_FUTURES_BASE_URL,
        path='/fapi/v1/time',
        params={},
    )
    return int(data['serverTime'])


def check_server_time_drift() -> dict:
    server_time_ms = get_binance_server_time_ms()
    local_time_ms = int(time.time() * 1000)
    drift_ms = local_time_ms - server_time_ms
    return {
        'server_time_ms': server_time_ms,
        'local_time_ms': local_time_ms,
        'drift_ms': drift_ms,
        'ok': abs(drift_ms) <= SERVER_TIME_DRIFT_LIMIT_MS,
        'limit_ms': SERVER_TIME_DRIFT_LIMIT_MS,
    }


def check_binance_permissions(api_key: str, secret_key: str) -> dict:
    return binance_signed_request('GET', BINANCE_SPOT_BASE_URL, '/sapi/v1/account/apiRestrictions', api_key, secret_key)


def get_futures_balance_raw(api_key: str, secret_key: str):
    return binance_signed_request('GET', BINANCE_FUTURES_BASE_URL, '/fapi/v2/balance', api_key, secret_key)


def get_futures_positions_raw(api_key: str, secret_key: str, symbol: Optional[str] = None):
    params = {}
    if symbol:
        params['symbol'] = symbol.upper()
    return binance_signed_request('GET', BINANCE_FUTURES_BASE_URL, '/fapi/v2/positionRisk', api_key, secret_key, params=params)


def get_futures_open_orders_raw(api_key: str, secret_key: str, symbol: str):
    return binance_signed_request('GET', BINANCE_FUTURES_BASE_URL, '/fapi/v1/openOrders', api_key, secret_key, params={'symbol': symbol.upper()})


def cancel_futures_order_raw(api_key: str, secret_key: str, symbol: str, order_id: int):
    return binance_signed_request('DELETE', BINANCE_FUTURES_BASE_URL, '/fapi/v1/order', api_key, secret_key, params={'symbol': symbol.upper(), 'orderId': order_id})


def cancel_all_futures_open_orders_raw(api_key: str, secret_key: str, symbol: str):
    return binance_signed_request('DELETE', BINANCE_FUTURES_BASE_URL, '/fapi/v1/allOpenOrders', api_key, secret_key, params={'symbol': symbol.upper()})


def get_futures_price(symbol: str) -> float:
    data = binance_public_get(BINANCE_FUTURES_BASE_URL, '/fapi/v1/ticker/price', params={'symbol': symbol.upper()})
    return float(data['price'])


def get_futures_exchange_info():
    return binance_public_get(BINANCE_FUTURES_BASE_URL, '/fapi/v1/exchangeInfo', params={})


@lru_cache(maxsize=256)
def get_symbol_filters(symbol: str) -> dict:
    symbol = symbol.upper()
    exchange_info = get_futures_exchange_info()
    for item in exchange_info.get('symbols', []):
        if item.get('symbol') == symbol:
            filters = {f.get('filterType'): f for f in item.get('filters', [])}
            lot_size = filters.get('LOT_SIZE', {})
            price_filter = filters.get('PRICE_FILTER', {})
            min_notional = filters.get('MIN_NOTIONAL', {})
            return {
                'symbol': symbol,
                'quantity_precision': int(item.get('quantityPrecision', 3)),
                'price_precision': int(item.get('pricePrecision', 2)),
                'step_size': lot_size.get('stepSize', '0.001'),
                'min_qty': lot_size.get('minQty', '0.001'),
                'tick_size': price_filter.get('tickSize', '0.01'),
                'min_price': price_filter.get('minPrice', '0.01'),
                'min_notional': min_notional.get('notional', '5'),
            }
    raise HTTPException(status_code=400, detail=f'지원하지 않는 심볼입니다: {symbol}')


def floor_to_step(value: float, step: str) -> float:
    value_decimal = Decimal(str(value))
    step_decimal = Decimal(str(step))
    if step_decimal <= 0:
        return float(value_decimal)
    return float((value_decimal / step_decimal).to_integral_value(rounding=ROUND_DOWN) * step_decimal)


def normalize_quantity(symbol: str, quantity: float) -> float:
    filters = get_symbol_filters(symbol)
    normalized = floor_to_step(quantity, filters['step_size'])
    min_qty = float(filters['min_qty'])
    if normalized < min_qty:
        return 0.0
    return normalized


def normalize_price(symbol: str, price: float) -> float:
    filters = get_symbol_filters(symbol)
    normalized = floor_to_step(price, filters['tick_size'])
    min_price = float(filters['min_price'])
    if normalized < min_price:
        return 0.0
    return normalized


def is_min_notional_ok(symbol: str, quantity: float, price: float) -> bool:
    filters = get_symbol_filters(symbol)
    return quantity * price >= float(filters['min_notional'])


def get_futures_klines(symbol: str, interval: str = '1m', limit: int = 1500, start_time: int | None = None, end_time: int | None = None):
    params = {'symbol': symbol.upper(), 'interval': interval, 'limit': limit}
    if start_time is not None:
        params['startTime'] = start_time
    if end_time is not None:
        params['endTime'] = end_time
    return binance_public_get(BINANCE_FUTURES_BASE_URL, '/fapi/v1/klines', params=params)


def get_futures_klines_as_df(symbol: str, interval: str = '1m', limit: int = 1500) -> pd.DataFrame:
    data = get_futures_klines(symbol=symbol, interval=interval, limit=limit)
    rows = []
    for item in data:
        rows.append({'timestamp': int(item[0]), 'open': float(item[1]), 'high': float(item[2]), 'low': float(item[3]), 'close': float(item[4]), 'volume': float(item[5])})
    return pd.DataFrame(rows)


def get_usdt_available_balance(api_key: str, secret_key: str) -> float:
    balances = get_futures_balance_raw(api_key, secret_key)
    for item in balances:
        if item.get('asset') == 'USDT':
            return float(item.get('availableBalance', '0'))
    return 0.0


def get_active_position(api_key: str, secret_key: str, symbol: str) -> Optional[dict]:
    positions = get_futures_positions_raw(api_key, secret_key, symbol=symbol)
    if isinstance(positions, dict):
        positions = [positions]
    for position in positions:
        if abs(float(position.get('positionAmt', '0'))) > 0:
            return position
    return None


def calc_open_position_value_from_binance_position(position: Optional[dict]) -> float:
    if not position:
        return 0.0
    position_amt = abs(float(position.get('positionAmt', '0')))
    mark_price = float(position.get('markPrice', '0'))
    return position_amt * mark_price


def futures_set_leverage_raw(api_key: str, secret_key: str, symbol: str, leverage: int):
    return binance_signed_request('POST', BINANCE_FUTURES_BASE_URL, '/fapi/v1/leverage', api_key, secret_key, params={'symbol': symbol.upper(), 'leverage': leverage})


def futures_market_order_raw(api_key: str, secret_key: str, symbol: str, side: Literal['BUY', 'SELL'], quantity: float, reduce_only: bool = False, new_client_order_id: Optional[str] = None):
    params = {'symbol': symbol.upper(), 'side': side, 'type': 'MARKET', 'quantity': quantity}
    if reduce_only:
        params['reduceOnly'] = 'true'
    if new_client_order_id:
        params['newClientOrderId'] = new_client_order_id
    return binance_signed_request('POST', BINANCE_FUTURES_BASE_URL, '/fapi/v1/order', api_key, secret_key, params=params)


def futures_stop_market_order_raw(api_key: str, secret_key: str, symbol: str, side: Literal['BUY', 'SELL'], stop_price: float, new_client_order_id: Optional[str] = None):
    params = {'symbol': symbol.upper(), 'side': side, 'type': 'STOP_MARKET', 'stopPrice': normalize_price(symbol, stop_price), 'closePosition': 'true', 'workingType': 'MARK_PRICE'}
    if new_client_order_id:
        params['newClientOrderId'] = new_client_order_id
    return binance_signed_request('POST', BINANCE_FUTURES_BASE_URL, '/fapi/v1/order', api_key, secret_key, params=params)


def futures_take_profit_market_order_raw(api_key: str, secret_key: str, symbol: str, side: Literal['BUY', 'SELL'], stop_price: float, new_client_order_id: Optional[str] = None):
    params = {'symbol': symbol.upper(), 'side': side, 'type': 'TAKE_PROFIT_MARKET', 'stopPrice': normalize_price(symbol, stop_price), 'closePosition': 'true', 'workingType': 'MARK_PRICE'}
    if new_client_order_id:
        params['newClientOrderId'] = new_client_order_id
    return binance_signed_request('POST', BINANCE_FUTURES_BASE_URL, '/fapi/v1/order', api_key, secret_key, params=params)
