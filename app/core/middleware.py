import time
from collections import defaultdict, deque

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.core.config import RATE_LIMIT_PER_MINUTE


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Permissions-Policy'] = 'geolocation=(), microphone=(), camera=()'
        return response


class SimpleRateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app):
        super().__init__(app)
        self.requests = defaultdict(deque)

    async def dispatch(self, request: Request, call_next):
        if request.url.path in ('/', '/health'):
            return await call_next(request)

        identifier = request.client.host if request.client else 'unknown'
        now = time.time()
        window_start = now - 60
        bucket = self.requests[identifier]

        while bucket and bucket[0] < window_start:
            bucket.popleft()

        if len(bucket) >= RATE_LIMIT_PER_MINUTE:
            return JSONResponse(
                status_code=429,
                content={'detail': '요청이 너무 많습니다. 잠시 후 다시 시도하세요.'},
            )

        bucket.append(now)
        return await call_next(request)
