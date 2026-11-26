# 미니PC 테스트넷 검증 서버

검증 서버는 별도 Docker Compose 프로젝트입니다. 공개 서비스는 기존
`trade-server.duckdns.org`를 사용합니다. 검증 서버 주소는 Tailscale 노드를
등록한 뒤 `https://trade-staging.<tailnet>.ts.net` 형식으로 확정합니다.

| 항목 | 운영 (현재 main 배포 기준) | 검증 |
| --- | --- | --- |
| Compose 프로젝트 | `trade-server-backend` 및 자동배포의 `trade-backend-release-*` | `trade-staging` |
| 컨테이너 | `trade-backtest-api`, 기존 Compose의 `trade-api`, `trade-postgres` | `trade-staging-api`, `trade-staging-postgres`, `trade-staging-nginx`, `trade-staging-tailscale` |
| Docker 네트워크 | `trade-server-backend_default` | `trade-staging_staging-web`, `trade-staging_staging-db` |
| DB 및 볼륨 | 운영 Compose의 DB 및 `postgres-data` | DB/사용자 `trade_staging`, 볼륨 `trade-staging_staging-postgres` |
| 웹/DB 포트 | 호스트 80·443, API `127.0.0.1:8000`, DB 매핑은 현장 확인 | 호스트에 공개한 포트 없음. Tailscale 노드 443, 노드 네임스페이스의 루프백 18080, 컨테이너 API 8000·DB 5432 |
| Nginx와 인증서 | 호스트 Nginx와 Certbot | 검증 컨테이너 Nginx와 Tailscale Serve의 HTTPS 인증서 |

운영 포트 매핑과 네트워크 근거는 백엔드 main의
`.github/workflows/deploy-mini-pc.yml` 27–63줄과 `docker-compose.yml`입니다.
운영 시스템 전체의 Docker 인벤토리는 설치 전에 미니PC에서 확인합니다.

## 접근 방식

| 방식 | 설정과 노출 |
| --- | --- |
| **권장 · Tailscale Serve** | 전용 노드와 접근 정책이 필요합니다. 현재 운영 Nginx·Certbot·공유기 설정 변경 없이 승인된 tailnet 사용자만 방문할 수 있습니다. 인증서는 MagicDNS 및 HTTPS 활성화 후 Serve가 요청합니다. 인증서 투명성 로그에는 노드의 인증서 이름이 공개됩니다. |
| 공개 DuckDNS + Certbot | 외부 접근이 필요한 경우 검토합니다. `staging.trade-server.duckdns.org`의 A·AAAA가 실제 미니PC와 같은 공인 IP로 해석되는지 확인 필요입니다. 확인되지 않으면 별도로 등록한 `trade-staging.duckdns.org`를 검토합니다. HTTP-01은 외부에서 접근 가능한 TCP 80의 별도 검증 경로가 필요하고, DNS-01은 해당 이름의 TXT를 갱신할 수 있어야 합니다. 공개 방식은 별도 호스트 Nginx 가상 호스트와 접근 제어를 승인받은 뒤 적용합니다. 현재 구성의 18080은 컨테이너 내부 전용입니다. |

공개 방식을 별도 승인받는 경우에만 아래 순서로 진행합니다.

1. `dig +short staging.trade-server.duckdns.org A`와
   `dig +short staging.trade-server.duckdns.org AAAA`로 레코드를 확인합니다.
   사용할 실제 호스트명으로 바꿔 실행하며, 레코드가 운영과 같은 공인 IP를
   가리키고 외부 TCP 80이 도달하는지 확인합니다.
2. 기존 사이트와 분리된 호스트 Nginx 가상 호스트에서
   `/.well-known/acme-challenge/`만 별도 웹루트
   `/var/www/trade-staging-acme`로 제공하도록 변경안을 검토합니다.
   그 설정이 적용된 뒤 `sudo certbot certonly --webroot -w /var/www/trade-staging-acme -d '<확정된 검증 호스트명>'`
   으로 발급하고 `sudo certbot renew --dry-run`으로 갱신을 검증합니다.
   공개 TLS 프록시 연결은 별도 변경으로 준비합니다.

## 미니PC에서 설치

Docker와 Compose v2가 설치되어 있고 현재 계정에서 Docker를 사용할 수 있다고
가정합니다. 아래는 승인 후 **재준님이 직접** 실행할 절차입니다.
작업은 기존 배포 디렉터리 밖의 새 폴더에서 합니다.

### 1. 파일 준비 · 약 5분

```bash
umask 077
mkdir -p "$HOME/trade-staging"
cd "$HOME/trade-staging"
git clone --branch feature/2026-11-27 https://github.com/jaelejoon/Trade_Server_Backend.git config
git clone https://github.com/jaelejoon/Trade_Server_Backend.git backend
git clone https://github.com/jaelejoon/Trade_Server_Frontend.git frontend
mkdir -p config/staging-dist
cp -n config/.env.staging.example .env.staging
chmod 600 .env.staging
ss -ltn
docker ps -a --format '{{.Names}} {{.Ports}}'
docker network ls --format '{{.Name}}'
docker volume ls --format '{{.Name}}'
free -h
df -h
```

검증 표의 이름이 다른 프로젝트에 이미 점유되어 있으면 설치를 멈춥니다.
Tailscale 관리자 화면에서 MagicDNS와 HTTPS 인증서를 켜고, 전용 검증 노드의
443이 허용된 사용자에게만 열리도록 tailnet 정책을 먼저 확인합니다.
가입에 사용할 검증 노드 전용 1회용 인증 키를 생성합니다.

### 2. 검증 후보 지정 및 빌드 · 약 10–20분

백엔드 PR #1 이상이 포함된 검토 완료 SHA와 호환되는 프런트 SHA를
아래 명령의 자리표시에 넣습니다. 브랜치 예시는 백엔드
`feature/2026-11-26-strategies-1x`입니다. 프런트 SHA는 승인된 PR의 최신
커밋을 사용합니다. 각각 SHA가 확인되지 않으면 빌드하지 않습니다.

```bash
cd "$HOME/trade-staging"
git -C backend fetch origin feature/2026-11-26-strategies-1x
git -C backend switch --detach '<승인된 백엔드 전체 SHA>'
git -C frontend fetch origin
git -C frontend switch --detach '<승인된 프런트 전체 SHA>'
git -C backend rev-parse HEAD
git -C frontend rev-parse HEAD
nano .env.staging
```

`.env.staging`의 `STAGING_BACKEND_SHA`에 확인한 백엔드 SHA를 넣습니다.
검증 전용 DB 비밀번호(48자 이상 영숫자), JWT 키(32바이트 이상),
Fernet 키(32바이트 원본의 URL-safe Base64), Tailscale 인증 키를
해당 변수에 직접 입력합니다. 운영 키는 재사용하지 않습니다.
이 백엔드는 Binance 키를 `.env`에서 읽지 않고 계정별로 관리합니다.
캔들·백테스트 검증에는 거래 키가 필요 없으며, API 키 등록이 필요한
별도 검증에서만 재준님이 **검증 웹의 계정 설정**에 테스트넷 키를 직접 등록합니다.

다음 두 빌드는 순서대로 실행합니다. 프런트 빌드에 미니PC의 비밀 파일은
마운트하지 않습니다. 프런트 저장소에 접근할 수 있는 GitHub 계정이 필요합니다.

```bash
cd "$HOME/trade-staging"
staging_sha=$(git -C backend rev-parse HEAD)
docker build -t "trade-staging-api:$staging_sha" backend
docker run --rm --cpus 1 --memory 1g --user "$(id -u):$(id -g)" \
  -e npm_config_cache=/tmp/npm-cache -e VITE_API_BASE_URL=/api \
  -v "$PWD/frontend:/app" -w /app node:22-bookworm \
  sh -c 'npm ci && npm run build'
test -s frontend/dist/index.html && rsync -r --delay-updates frontend/dist/ config/staging-dist/
```

### 3. 검증 서버 기동 및 확인 · 약 10분

```bash
cd "$HOME/trade-staging"
staging_compose() {
  env -u STAGING_BACKEND_SHA -u STAGING_DB_PASSWORD -u STAGING_JWT_SECRET_KEY \
    -u STAGING_ENCRYPTION_KEY -u STAGING_TS_AUTHKEY \
    docker compose --env-file "$HOME/trade-staging/.env.staging" \
    -f "$HOME/trade-staging/config/compose.staging.yml" "$@"
}
staging_compose config --quiet
staging_compose up -d --wait --wait-timeout 120
staging_compose exec staging-nginx nginx -t
staging_compose exec staging-tailscale tailscale serve --bg --https=443 http://127.0.0.1:18080
staging_compose exec staging-tailscale tailscale serve status
staging_compose ps
```

에러가 발생하면 그 단계에서 멈춥니다. `config`를 `--quiet` 없이
실행하거나 컨테이너 환경 전체를 출력하면 비밀값이 노출될 수 있습니다.
첫 등록 후 Tailscale 인증 키를 로컬 설정 파일에서 지워도 등록 상태는
전용 볼륨에 남습니다. 이전 키가 들어 있는 컨테이너가 교체되기 전까지는
해당 컨테이너 설정에도 그 키가 남을 수 있으므로 1회용 키를 사용합니다.

Serve가 표시한 HTTPS 주소를 Tailscale에 로그인한 PC에서 엽니다.
`/api/health`의 `status=ok`, `dry_run=true`, `binance_testnet=true`와
`/api/health/db`를 확인합니다. 검증 계정으로 가입·로그인·세션 갱신을
확인한 뒤 캔들 조회와 짧은 범위의 백테스트를 실행합니다.
허용되지 않은 기기에서도 접근 차단을 확인합니다.

PR #1의 `app/core/config.py` 126–138줄은 테스트넷 모드에서 실거래소
선물 URL이 들어오면 예외를 발생시킵니다. Compose는 테스트넷 선물·현물
URL과 모의 실행 플래그를 고정합니다. 시작 래퍼는 앱의 실제 설정을 읽어
다시 확인하며, PR #1 가드가 없는 기존 main 이미지도 앱 시작 전에 거부합니다.
전용 DB는 운영 백업이나 운영 API 키를 복사하지 않은 빈 상태에서 시작합니다.

## 배포, 자원 및 되돌리기

처음에는 위 절차로 검토된 SHA만 수동 반영합니다. 자동화할 때는
별도 러너 라벨의 수동 `workflow_dispatch`에서 허용된 후보 SHA를 입력받아
검증 프로젝트에만 배포하는 방식을 제안합니다. 운영 main 배포와 트리거를
공유하지 않습니다. 검증 통과 후 검증한 코드와 병합 결과를 대조하고
운영 승인을 받은 뒤 main으로 넘깁니다.

Compose 상한 합계는 약 2.5 vCPU·2.5 GiB입니다. PostgreSQL·이미지·프런트
빌드 캐시에는 여유 디스크 20 GiB를 초기 기준으로 확보하고, 검증 DB에는
필요 기간의 캔들만 넣습니다. 이 수치는 디스크 쿼터가 아니므로 사용량을
확인합니다. 두 환경이 같은 Docker 데몬·디스크·호스트를 공유하고,
후보 이미지 빌드와 백테스트가 겹치면 운영 응답이 느려질 수 있습니다.

이전 검증 이미지 SHA와 프런트 SHA를 기록해 둡니다. 문제가 생기면
`.env.staging`의 SHA를 이전 검증 이미지로 돌리고,
`staging_compose up -d --no-deps staging-api`를 실행합니다.
프런트는 이전 SHA로 2단계 빌드·복사를 반복합니다. DB 호환성이 확인되지
않았거나 검증 서버가 필요 없어지면, 3단계의 함수가 정의된 셸에서
`staging_compose down`을 실행해 검증 컨테이너만 내립니다. 검증 DB와
Tailscale 상태 볼륨은 보존됩니다.
