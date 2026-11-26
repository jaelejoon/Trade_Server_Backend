# 보안 강화 업데이트

## 포함 변경

- 로그인 실패 잠금 테이블 추가: `login_attempts`
- 로그인 실패 5회 시 기본 15분 잠금
- `/auth/login`, `/auth/register`, `/auth/swagger-login` 별도 rate limit
- 쓰기 요청 POST/PUT/PATCH/DELETE 별도 rate limit
- 요청 body 크기 제한
- 프록시/Nginx 환경에서 `X-Forwarded-For`, `X-Real-IP` 사용 옵션
- 운영 환경에서 Swagger 자동 비활성화 기본값 적용
- 운영 환경에서 기본 JWT_SECRET_KEY 사용 차단
- 운영 환경에서 `ALLOWED_ORIGINS=*` 차단
- 보안 헤더 강화

---

# 적용 파일

아래 파일을 같은 경로에 덮어쓰기:

```text
trade-server-backend/app/core/config.py
trade-server-backend/app/core/middleware.py
trade-server-backend/app/models/login_attempt.py
trade-server-backend/app/models/__init__.py
trade-server-backend/app/services/login_security_service.py
trade-server-backend/app/routers/auth.py
trade-server-backend/app/main.py
trade-server-backend/.env.example
```

---

# 로컬 개발 .env 예시

```env
ENVIRONMENT=dev
DISABLE_DOCS=false
TRUST_PROXY_HEADERS=false

ACCESS_TOKEN_EXPIRE_MINUTES=1440

LOGIN_FAILURE_LIMIT=5
LOGIN_LOCK_MINUTES=15
LOGIN_FAILURE_WINDOW_MINUTES=15

AUTH_RATE_LIMIT_PER_MINUTE=10
WRITE_RATE_LIMIT_PER_MINUTE=30
RATE_LIMIT_PER_MINUTE=120

MAX_REQUEST_BODY_BYTES=1048576
```

---

# 운영 .env 권장

```env
ENVIRONMENT=prod
DISABLE_DOCS=true
TRUST_PROXY_HEADERS=true

ACCESS_TOKEN_EXPIRE_MINUTES=15

LOGIN_FAILURE_LIMIT=5
LOGIN_LOCK_MINUTES=15
LOGIN_FAILURE_WINDOW_MINUTES=15

AUTH_RATE_LIMIT_PER_MINUTE=10
WRITE_RATE_LIMIT_PER_MINUTE=30
RATE_LIMIT_PER_MINUTE=120

MAX_REQUEST_BODY_BYTES=1048576
```

---

# 적용 후 실행

## 로컬 개발

```powershell
uvicorn app.main:app --reload
```

## Docker Compose 운영 모드

```powershell
docker compose -f docker-compose.prod.yml up -d --build
```

---

# DB 관련 참고사항

현재 프로젝트는:

```python
Base.metadata.create_all()
```

구조를 사용하므로 새 테이블:

```text
login_attempts
```

는 자동 생성됩니다.

이번 업데이트는:

- 새 테이블 추가만 존재
- 기존 테이블 컬럼 변경 없음
- 기존 데이터 마이그레이션 없음

이므로 비교적 안전하게 적용 가능합니다.

---

# 운영 보안 권장사항

운영 환경에서는 반드시 아래 조건을 권장합니다.

## HTTPS 사용

- Cloudflare
- Nginx SSL
- Reverse Proxy HTTPS

중 최소 1개 적용.

---

## Swagger 비활성화

운영 환경에서는:

```env
DISABLE_DOCS=true
```

권장.

---

## JWT Secret 변경

기본 JWT 시크릿 사용 금지.

충분히 긴 랜덤 문자열 사용 권장.

예시:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

---

## ALLOWED_ORIGINS 제한

운영에서는 절대:

```env
ALLOWED_ORIGINS=*
```

사용 금지.

예시:

```env
ALLOWED_ORIGINS=https://your-domain.com
```

---

# 추가 예정 보안 항목

향후 적용 권장:

- HttpOnly Secure Cookie 기반 JWT
- Refresh Token 분리
- Cloudflare WAF
- 관리자 권한 분리
- 계좌 소유자 검증 강화
- CAPTCHA / Turnstile
- API 요청 서명 강화
- 감사 로그(Audit Log)
- IP 기반 이상행위 탐지

# Account Owner 권한검사 보강 패치

## 포함 변경

- `get_active_trading_account_owned_by_user()` 공통 헬퍼 추가
- `trading_account_id` 기반 실행 API에서 활성 계정 + 소유자 검증을 한 번에 수행
- `/trading/logs`에 `trading_account_id` 필터 추가 시, 먼저 해당 계좌가 현재 로그인 사용자 소유인지 검증
- `/trading/error-logs`에 `trading_account_id` 필터 추가 시, 먼저 해당 계좌가 현재 로그인 사용자 소유인지 검증
- 계좌/API Key 미소유 접근 시 404로 응답해서 다른 사용자의 리소스 존재 여부를 노출하지 않도록 유지

## 적용 파일

아래 파일을 같은 경로에 덮어쓰기:

```text
trade-server-backend/app/services/account_service.py
trade-server-backend/app/routers/trading.py
```

## 적용 후 실행

```powershell
uvicorn app.main:app --reload
```

Docker Compose 운영 모드라면:

```powershell
docker compose -f docker-compose.prod.yml up -d --build
```

## 확인 방법

1. 사용자 A로 로그인해서 계정 생성
2. 사용자 B로 로그인
3. 사용자 A의 `trading_account_id`로 아래 API 호출

```text
POST /trading/accounts/{trading_account_id}/start
POST /trading/accounts/{trading_account_id}/stop
POST /trading/accounts/{trading_account_id}/reconcile
DELETE /trading/accounts/{trading_account_id}
GET /trading/logs?trading_account_id={trading_account_id}
GET /trading/error-logs?trading_account_id={trading_account_id}
```

정상 방어 시 `404`가 떠야 합니다.
