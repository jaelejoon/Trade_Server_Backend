import time
from collections import defaultdict, deque
from typing import Iterable

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.core.config import (
    AUTH_RATE_LIMIT_PER_MINUTE,
    MAX_REQUEST_BODY_BYTES,
    RATE_LIMIT_BLOCK_SECONDS,
    RATE_LIMIT_PER_MINUTE,
    REQUIRE_HTTPS_AUTH,
    TRUST_PROXY_HEADERS,
    WRITE_RATE_LIMIT_PER_MINUTE,
)

AUTH_PATHS = {
    '/auth/login',
    '/auth/register',
    '/auth/swagger-login',
}

SAFE_PATHS = {
    '/',
    '/health',
}

WRITE_METHODS = {
    'POST',
    'PUT',
    'PATCH',
    'DELETE',
}



def is_local_request(request: Request) -> bool:
    host = request.url.hostname or ''
    client_host = request.client.host if request.client else ''

    return host in {'localhost', '127.0.0.1'} or client_host in {'127.0.0.1', '::1'}


def is_https_request(request: Request) -> bool:
    if request.url.scheme == 'https':
        return True

    if TRUST_PROXY_HEADERS:
        forwarded_proto = request.headers.get('x-forwarded-proto', '')
        if forwarded_proto.split(',')[0].strip().lower() == 'https':
            return True

        forwarded_ssl = request.headers.get('x-forwarded-ssl', '')
        if forwarded_ssl.lower() == 'on':
            return True

    return False


class HttpsAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if (
            REQUIRE_HTTPS_AUTH
            and request.url.path in AUTH_PATHS
            and not is_local_request(request)
            and not is_https_request(request)
        ):
            return JSONResponse(
                status_code=403,
                content={'detail': '인증 요청은 HTTPS에서만 허용됩니다.'},
            )

        return await call_next(request)

def get_client_ip(request: Request) -> str:
    if TRUST_PROXY_HEADERS:
        forwarded_for = request.headers.get('x-forwarded-for')
        if forwarded_for:
            return forwarded_for.split(',')[0].strip()

        real_ip = request.headers.get('x-real-ip')
        if real_ip:
            return real_ip.strip()

    return request.client.host if request.client else 'unknown'


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Permissions-Policy'] = 'geolocation=(), microphone=(), camera=()'
        response.headers['Cross-Origin-Opener-Policy'] = 'same-origin'
        response.headers['Cross-Origin-Resource-Policy'] = 'same-origin'
        response.headers['Cache-Control'] = 'no-store'
        return response


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        content_length = request.headers.get('content-length')

        if content_length is not None:
            try:
                if int(content_length) > MAX_REQUEST_BODY_BYTES:
                    return JSONResponse(
                        status_code=413,
                        content={'detail': '요청 본문이 너무 큽니다.'},
                    )
            except ValueError:
                return JSONResponse(
                    status_code=400,
                    content={'detail': '잘못된 Content-Length 헤더입니다.'},
                )

        return await call_next(request)


class SimpleRateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app):
        super().__init__(app)
        self.requests = defaultdict(deque)
        self.blocked_until = {}

    async def dispatch(self, request: Request, call_next):
        if request.url.path in SAFE_PATHS:
            return await call_next(request)

        identifier = self._identifier(request)
        now = time.time()

        blocked_until = self.blocked_until.get(identifier, 0)
        if blocked_until > now:
            return JSONResponse(
                status_code=429,
                content={'detail': '요청이 너무 많습니다. 잠시 후 다시 시도하세요.'},
            )

        limit = self._limit_for_request(request)
        bucket = self.requests[identifier]
        window_start = now - 60

        while bucket and bucket[0] < window_start:
            bucket.popleft()

        if len(bucket) >= limit:
            self.blocked_until[identifier] = now + RATE_LIMIT_BLOCK_SECONDS
            return JSONResponse(
                status_code=429,
                content={'detail': '요청이 너무 많습니다. 잠시 후 다시 시도하세요.'},
            )

        bucket.append(now)
        return await call_next(request)

    def _identifier(self, request: Request) -> str:
        ip = get_client_ip(request)
        path_group = 'auth' if request.url.path in AUTH_PATHS else 'api'
        return f'{ip}:{path_group}'

    def _limit_for_request(self, request: Request) -> int:
        if request.url.path in AUTH_PATHS:
            return AUTH_RATE_LIMIT_PER_MINUTE

        if request.method.upper() in WRITE_METHODS:
            return WRITE_RATE_LIMIT_PER_MINUTE

        return RATE_LIMIT_PER_MINUTE
