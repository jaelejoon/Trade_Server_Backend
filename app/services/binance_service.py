import hashlib
import hmac
import logging
import time
from typing import Literal, Optional
from urllib.parse import urlencode

import requests
from fastapi import HTTPException

from app.core.config import (
    BINANCE_MAX_RETRIES,
    BINANCE_REQUEST_TIMEOUT_SECONDS,
    BINANCE_RETRY_BASE_SLEEP_SECONDS,
    BINANCE_SPOT_BASE_URL,
    SERVER_TIME_DRIFT_LIMIT_MS,
    DRY_RUN,
    ENABLE_LIVE_TRADING,
)
from app.core.security import decrypt_text
from app.models.binance_api_key import BinanceApiKey

# urllib3 DEBUG 로그에는 서명된 요청 경로가 포함될 수 있다.
logging.getLogger('urllib3.connectionpool').setLevel(logging.INFO)

RETRYABLE_BINANCE_CODES = {-1001, -1007, -1021}


class OrderExecutionUnknown(RuntimeError):
    """주문 결과가 불명확하다. 상태 확인 전에는 주문을 다시 보내지 않는다."""


def get_decrypted_api_keys(item: BinanceApiKey):
    try:
        return decrypt_text(item.api_key_encrypted), decrypt_text(item.secret_key_encrypted)
    except Exception as exc:
        raise HTTPException(status_code=500, detail="API Key 복호화에 실패했습니다.") from exc


def _parse_response(response: requests.Response):
    try:
        return response.json()
    except ValueError:
        return None


def _binance_error_code(data) -> Optional[int]:
    code = data.get('code') if isinstance(data, dict) else None
    return code if type(code) is int else None


def _raise_binance_error(response: requests.Response, data):
    raise HTTPException(
        status_code=400,
        detail={
            'message': 'Binance API 요청 실패',
            'binance_status': response.status_code,
            'binance_code': _binance_error_code(data),
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
    allowed_paths = {
        "GET": {"/api/v3/account", "/api/v3/order", "/api/v3/orderList",
                "/api/v3/openOrderList", "/sapi/v1/account/apiRestrictions"},
        "POST": {"/api/v3/order", "/api/v3/orderList/oco"},
    }
    if base_url != BINANCE_SPOT_BASE_URL or path not in allowed_paths.get(method, set()):
        raise HTTPException(status_code=403, detail="현물 요청 경로가 허용되지 않습니다.")
    params = params or {}
    if method != "GET":
        if DRY_RUN or not ENABLE_LIVE_TRADING:
            raise HTTPException(status_code=403, detail="현물 쓰기 요청이 허용되지 않습니다.")
        if params.get("symbol") != "BTCUSDT":
            raise HTTPException(status_code=400, detail="BTCUSDT만 주문할 수 있습니다.")

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
                response = requests.get(url, headers=headers, timeout=BINANCE_REQUEST_TIMEOUT_SECONDS, allow_redirects=False)
            elif method == 'POST':
                response = requests.post(url, headers=headers, timeout=BINANCE_REQUEST_TIMEOUT_SECONDS, allow_redirects=False)
            elif method == 'DELETE':
                response = requests.delete(url, headers=headers, timeout=BINANCE_REQUEST_TIMEOUT_SECONDS, allow_redirects=False)
            else:
                raise ValueError('지원하지 않는 HTTP method입니다.')
        except requests.RequestException:
            if method != "GET":
                raise OrderExecutionUnknown("Binance 쓰기 요청 결과를 확인할 수 없습니다.") from None
            if attempt < BINANCE_MAX_RETRIES - 1:
                time.sleep(BINANCE_RETRY_BASE_SLEEP_SECONDS * (2 ** attempt))
                continue
            raise HTTPException(status_code=503, detail='Binance 네트워크 요청 실패') from None

        data = _parse_response(response)

        if 200 <= response.status_code < 300 and data is not None:
            return data

        if method != "GET" and (
            response.status_code == 408
            or response.status_code < 400
            or response.status_code >= 500
            or _binance_error_code(data) in {-1006, -1007}
        ):
            raise OrderExecutionUnknown("Binance 쓰기 요청 결과가 불명확합니다.")
        if 200 <= response.status_code < 300:
            raise HTTPException(status_code=502, detail='Binance 응답 형식이 올바르지 않습니다.')
        if method == "GET" and _is_retryable_response(response.status_code, data) and attempt < BINANCE_MAX_RETRIES - 1:
            time.sleep(BINANCE_RETRY_BASE_SLEEP_SECONDS * (2 ** attempt))
            continue

        _raise_binance_error(response, data)

    raise HTTPException(status_code=503, detail='Binance 요청 재시도 실패') from None


def binance_public_get(base_url: str, path: str, params: Optional[dict] = None):
    if params is None:
        params = {}

    for attempt in range(BINANCE_MAX_RETRIES):
        try:
            response = requests.get(
                f'{base_url}{path}',
                params=params,
                timeout=BINANCE_REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException:
            if attempt < BINANCE_MAX_RETRIES - 1:
                time.sleep(BINANCE_RETRY_BASE_SLEEP_SECONDS * (2 ** attempt))
                continue
            raise HTTPException(status_code=503, detail='Binance Public API 네트워크 요청 실패') from None

        data = _parse_response(response)
        if 200 <= response.status_code < 300:
            if data is None:
                raise HTTPException(status_code=502, detail='Binance 응답 형식이 올바르지 않습니다.')
            return data

        if _is_retryable_response(response.status_code, data) and attempt < BINANCE_MAX_RETRIES - 1:
            time.sleep(BINANCE_RETRY_BASE_SLEEP_SECONDS * (2 ** attempt))
            continue

        _raise_binance_error(response, data)

    raise HTTPException(status_code=503, detail='Binance Public API 재시도 실패') from None


def get_binance_server_time_ms() -> int:
    data = binance_public_get(
        base_url=BINANCE_SPOT_BASE_URL,
        path='/api/v3/time',
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


def get_spot_klines(symbol: str, interval: str = '1m', limit: int = 1000, start_time: int | None = None, end_time: int | None = None):
    params = {'symbol': symbol.upper(), 'interval': interval, 'limit': limit}
    if start_time is not None:
        params['startTime'] = start_time
    if end_time is not None:
        params['endTime'] = end_time
    return binance_public_get(BINANCE_SPOT_BASE_URL, '/api/v3/klines', params=params)
