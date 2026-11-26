# 현물 테스트넷 검증과 운영 전환

대상은 백엔드 [Draft #10][backend]과 프런트 [Draft #5][frontend]입니다.
[설치 절차](staging-mini-pc.md)로 별도 검증 스택을 먼저 준비합니다.
운영 선물 배포 순서와 기존 사전 점검 일정은 현물 전환 계획에 적용하지 않습니다.

## 주문 검증의 범위

PR #10의 `app/core/config.py`는 `BINANCE_TESTNET=true`이면
`BINANCE_SPOT_BASE_URL=https://testnet.binance.vision`만 허용합니다.
`app/services/binance_service.py`의 서명 요청에서 쓰기는 테스트넷이고,
`DRY_RUN=false`와 `ENABLE_LIVE_TRADING=true`를 모두 만족해야 통과합니다.
봇은 별도로 사용자·계정·전역 주문 설정과 실행 상태도 확인합니다.

이 검증 스택은 `DRY_RUN=true`, `ENABLE_LIVE_TRADING=false`를 고정합니다.
따라서 실제 테스트넷 매수·OCO·매도·재시작 주문 복구는 이 스택에서
**미검증**으로 남깁니다. 아래 주문 시나리오의 모의 테스트 결과와 실제
테스트넷 체결 검증을 각각 기록합니다. 실제 거래소 연동 체결을 배포 합격
기준에 포함하려면 별도의 승인된 테스트넷 전용 검증 단계가 필요합니다.

## 1. 웹과 읽기 경로

1. Tailscale에서 검증 주소를 열고 아래 경로의 응답을 확인합니다.
   `/api/health`는 `status=ok`, `dry_run=true`, `binance_testnet=true`,
   `/api/health/db`는 `status=ok`여야 합니다. 이 단계는 Binance 키가 필요 없습니다.
2. 검증 웹에서 검증 사용자로 가입·로그인합니다. 새로고침 후에도 로그인되고
   브라우저 개발자 도구의 Network에서 `GET /api/me`와 세션 갱신
   `POST /api/auth/refresh`가 성공해야 합니다. 요청 헤더·쿠키·본문은 복사하지 않습니다.
3. 백테스트 데이터 화면을 열어 `GET /api/backtests/data/status`와
   `GET /api/candles/1m/status/BTCUSDT`가 성공하는지 확인합니다.
   처음에는 0건이 정상입니다. 필요할 때만 검증 DB에 짧은 현물 캔들 구간을
   적재하고 화면에서 다시 조회합니다. 시간순·BTCUSDT·UTC 범위를 확인합니다.
4. 재준님이 검증 웹 **API 키 관리**에서 현물 테스트넷 전용 키를 직접
   등록한 뒤 **계정 설정**에서 검증 계좌를 추가합니다. 계정과 사용자의 모의
   실행 설정을 유지합니다. 등록 입력이나 키 화면은 공유하지 않습니다.
5. **계좌 현황**에서 잔고를 조회합니다.
   `GET /api/trading/accounts/{id}/portfolio`가 200이고 BTC·USDT 잔고가
   같은 테스트넷 계정의 잔고와 일치해야 합니다.
   다음 두 요청도 200이어야 합니다.
   - `GET /api/trading/accounts/{id}/fills?limit=20`
     새 계좌는 `items=[]`가 정상입니다.
   - `GET /api/trading/accounts/{id}/oco`
     새 계좌는 `oco=null`이고 화면에 ‘봇 보호주문 없음’이 표시되어야 합니다.
6. 스케줄러는 초기 `GET /api/trading/status`의
   `scheduler_enabled=false`와 화면의 꺼짐 표시가 일치해야 합니다.
   승인 후 [스케줄러 확인 절차](staging-mini-pc.md)에서 상태 표시 전환만
   확인하고 다시 끕니다. 중지 이력이 없는 새 계좌에서는 원인 미표시가
   정상입니다. 화면에 실제 중지 사유를 표시하는 검증에는 모의 원장/API
   응답을 사용해 원문과 표시가 일치하는지 확인합니다.

## 2. 주문·복구 시나리오

격리된 백엔드 소스 환경에서 아래 명령을 실행합니다. 거래소 호출은
기존 테스트의 모의 거래소로 대체되며 Binance 키가 필요 없습니다.

```bash
python -m pytest -q tests/test_spot_lifecycle.py tests/test_spot_service.py tests/test_spot_recovery_service.py tests/test_scheduler_guard.py
```

| 항목 | 통과 기준과 확인 위치 |
| --- | --- |
| 소액 시장가 매수 → 체결 → OCO | 모의 `test_entry_and_oco_exit`에서 매수 체결 원장이 한 번 기록되고 수수료 반영 후 봇 보유량에 OCO가 한 번 등록됩니다. 실제 확인 시 계좌 현황의 체결 및 보호주문, `/fills`·`/oco` 결과를 테스트넷 결과와 대조합니다. |
| 익절·손절 | 같은 모의 테스트의 `LIMIT_MAKER`·`STOP_LOSS` 각각에서 매도 원장이 한 번 기록되고 보호 상태가 정리됩니다. 실제 확인 시 반대 주문의 종료와 중복 매도 없음도 확인합니다. |
| OCO 실패·비상 매도 | `test_rejected_oco_sells_only_bot_quantity_then_stops`에서 확정 거절 시 봇 귀속량만 매도하고 계좌가 멈춥니다. `test_ambiguous_oco_stops_without_selling`에서는 상태가 불명확하면 추가 매도 없이 멈춰야 합니다. 실제 확인 시 체결 수량과 화면의 중지 사유를 대조합니다. |
| 검증 API 재시작 후 복구 | `test_restart_keeps_existing_oco`에서 기존 보호주문 ID를 재사용합니다. 검증 스택 재시작은 헬스체크와 원장 유지까지만 확인합니다. 실제 주문 복구 합격 기준은 재시작 전후 동일 OCO ID와 새 매수·보호주문 미생성입니다. |
| 재시도·중복 방지 | `test_oco_unknown_queries_without_resending`에서 제출 응답이 모호해도 기존 주문 조회로 확인하고 재전송하지 않아야 합니다. 실제 확인 시 거래소 주문 수 및 원장 거래 ID 중복 여부를 비교합니다. |
| UI 표시 | 계좌 현황에서 체결의 방향·수량·수수료, OCO 상태·수량·가격, 중지 사유와 스케줄러 표시가 API와 일치합니다. 체결 시각은 현재 응답에 없으므로 이 검증에서 기대하지 않습니다. |

## 3. 운영 반영 사전 점검

1. 검증한 백엔드·프런트 전체 SHA와 검증 결과를 기록합니다. 양쪽 CI와
   임시 PostgreSQL 복원본의 기동·로그인·스키마 보장 결과를 확인합니다.
   실체결 시나리오의 통과가 필수라면 미검증 상태에서는 승인을 보류합니다.
2. 재준님이 미니PC에서 **PR의 읽기 전용 스크립트**를 기존 API가 실행 중일
   때 실행합니다. 파일을 `/home/joon/trade-server-backend/`에 복사했다면
   명령은 아래와 같습니다. 미설치 컬럼으로 일부 SELECT가 실패할 수 있으며
   이때 ‘확인 불가’를 0으로 해석하지 않습니다.

   ```bash
   cd /home/joon/trade-server-backend && bash trade-predeploy-check.sh
   ```

3. 공유하는 결과에는 변수 이름·존재·적합 여부와 집계만 포함합니다.
   운영의 현물 모드 기준은 아래 표와 같습니다. `적합`은 스크립트가 보는
   형식·모드 기준이며 실제 자격증명이나 로그인 검증은 별도입니다.

| 이름 | 운영 배포 전 확인 |
| --- | --- |
| `JWT_SECRET_KEY` · `ENCRYPTION_KEY` · `DATABASE_URL` | 존재 필수. 각각 UTF-8 32바이트 이상, 유효한 Fernet 형식, PostgreSQL URL. 현재 암호화 키를 유지하고 DB 복원본에서 권한과 접속을 확인합니다. |
| `ALLOWED_ORIGINS` · `ENVIRONMENT` | 와일드카드 없이 실제 교차 출처만 허용. `ENVIRONMENT`는 운영 모드로 명시하는 편이 좋습니다. |
| `BINANCE_TESTNET` · `BINANCE_SPOT_BASE_URL` | 누락 시 운영 현물 기본값 사용. 명시할 경우 테스트넷 해제와 운영 현물 URL의 일치가 필요합니다. 기존 선물 URL 항목은 현물 코드에서 사용되지 않으므로 후속 정리 대상으로 기록합니다. |
| `DRY_RUN` · `ENABLE_LIVE_TRADING` | main 배포가 각각 모의 실행과 거래소 쓰기 비활성화를 강제합니다. |
| `ENABLE_TRADING_SCHEDULER` | 누락 시 기본 꺼짐. 현재 운영 워크플로는 따로 강제하지 않으므로 호스트 배포용 설정에서 누락 또는 명시적 꺼짐인지 점검합니다. |
| `REQUIRE_HTTPS_AUTH` · `TRUST_PROXY_HEADERS` · `AUTH_COOKIE_SECURE` · `AUTH_COOKIE_SAMESITE` | HTTPS 전달과 Secure 쿠키 적용 여부를 확인합니다. 운영 Nginx의 새로고침·`/api/auth/refresh` 쿠키 Path 변환은 배포 전후 확인합니다. |
| `VITE_API_BASE_URL` | 미니PC 프런트 빌드는 `/api` 사용. 별도 Vercel 배포가 활성화됐다면 프록시 대상을 점검합니다. |

4. DB 백업과 복원 방법을 확인한 뒤 기존 계정 상태를 점검합니다. 시작 시
   ORM은 `spot_candles_1m`, `spot_fills`, `backtest_runs`와 인증·설정·전략
   관련 누락 테이블을 생성합니다. 기존 테이블에는
   `bot_signal_states.runtime_state`, `users`의 모의 실행·권한·활성화·주문 설정
   컬럼 5개 및 `trading_accounts.dry_run`을 보완합니다. 관리자 또는
   최고관리자가 없으면 첫 사용자가 승격될 수 있으므로 계정 소유자를 먼저
   확인합니다. 선물 캔들·기존 계정과 레거시 컬럼은 삭제·변환되지 않습니다.
   현물 캔들은 새로 적재하고 현물 계정을 새로 만듭니다. 현물 이미지가
   실거래소 주소를 사용해 기동하더라도 이 버전의 쓰기 경로는 실거래소 주문을 거부합니다.
5. 승인된 한 배포 창에서 **백엔드 #10을 먼저** main에 반영합니다.
   API·DB 헬스체크와 모의 실행을 확인하고, 인증된 세션의
   `GET /api/trading/status`에서 `scheduler_enabled=false`를 확인합니다.
   로그인·세션 갱신과 현물 API를 확인한 뒤 **프런트 #5**를 반영합니다.
   두 저장소의 자동 배포는 각각 시작되므로 이전 프런트와 새 백엔드가
   잠깐 함께 있는 동안의 계좌 화면 동작도 검증 창에서 확인합니다.
   별도의 선물 PR을 순서대로 운영에 올리는 단계는 없습니다.
   PR #7은 선물 검증 구성의 기록이며, 현물 검증용 Draft의 설정 파일을 사용합니다.

## 4. 운영 되돌리기

- API 교체 중 실패하면 Actions의 이전 컨테이너 복구 결과와 공개 헬스를
  확인합니다. 복구가 끊겼다면 마지막 정상 이미지·컨테이너와 운영 설정을
  확인한 뒤 승인된 절차로 복구합니다.
- 이미 양쪽이 배포됐다면 호환성을 확인하며 프런트부터 역순으로 되돌리는
  PR을 사용합니다. 이전 선물 이미지는 현물 스케줄러 플래그를 인식하지
  않을 수 있습니다. 그 이미지로 되돌리기 전에 기존 실행 표시 계정과
  시작 시 작업 복구 동작을 확인하고 모의 실행·거래소 쓰기 차단을 유지합니다.
- 컨테이너 복구는 DB 변경을 되돌리지 않습니다. 추가 테이블과 원장은
  보존하고, 스키마나 데이터 복원이 꼭 필요할 때는 백업 시점과 이후 기록을
  검토해 별도 승인으로 진행합니다.

[backend]: https://github.com/jaelejoon/Trade_Server_Backend/pull/10
[frontend]: https://github.com/jaelejoon/Trade_Server_Frontend/pull/5
