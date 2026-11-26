# 트래픽 공격 1차 방어 적용 방법

이 구성은 FastAPI 앞에 Nginx를 두고 요청량, 동시 연결, 요청 본문 크기, 로그인 요청을 제한한다.

## 1. 적용 파일

```text
docker-compose.prod.yml
nginx/nginx.conf
nginx/conf.d/trade-api.conf
scripts/nginx-security-check.ps1
```

## 2. 운영 .env 권장값

```env
ENVIRONMENT=prod
DISABLE_DOCS=true
TRUST_PROXY_HEADERS=true
REQUIRE_HTTPS_AUTH=true
RATE_LIMIT_PER_MINUTE=120
AUTH_RATE_LIMIT_PER_MINUTE=10
WRITE_RATE_LIMIT_PER_MINUTE=30
RATE_LIMIT_BLOCK_SECONDS=60
MAX_REQUEST_BODY_BYTES=1048576
```

## 3. 실행 전 검사

```powershell
cd C:\Users\joon_\IdeaProjects\trade-server-backend
.\scripts\nginx-security-check.ps1
```

## 4. 실행

```powershell
docker compose -f docker-compose.prod.yml up -d --build
```

## 5. 확인

```powershell
docker ps
curl http://127.0.0.1/
```

## 6. 중요한 운영 원칙

- 외부에는 Nginx의 80/443만 열고, trade-api의 8000 포트는 직접 공개하지 않는다.
- 운영에서는 `DISABLE_DOCS=true`로 Swagger를 끈다.
- 로그인 API는 Nginx와 FastAPI 양쪽에서 제한한다.
- HTTPS는 Cloudflare, Nginx 인증서, 또는 배포 플랫폼 HTTPS 중 하나로 반드시 적용한다.
- Cloudflare를 쓰는 경우 WAF, Bot Fight Mode, Rate Limiting Rule을 추가로 켠다.

## 7. Cloudflare 추천 규칙

로그인 제한:

```text
URI Path contains /auth/login
10 requests per 1 minute per IP 초과 시 Block 또는 Managed Challenge
```

전체 API 제한:

```text
Hostname equals API 도메인
120 requests per 1 minute per IP 초과 시 Managed Challenge
```

국가 제한이 필요하면 한국 접속만 허용하는 규칙을 추가할 수 있다.
