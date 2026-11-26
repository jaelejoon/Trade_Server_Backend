# 현물 테스트넷 검증과 운영 전환

대상은 백엔드 [Draft #10][backend]과 프런트 [Draft #5][frontend]입니다.
[설치 절차](staging-mini-pc.md)로 별도 검증 스택을 먼저 준비합니다.
운영 선물 배포 순서와 기존 사전 점검 일정은 현물 전환 계획에 적용하지 않습니다.

## 1. 설치와 키 없이 확인

1. [설치 절차](staging-mini-pc.md)에 따라 이 검증 브랜치의 설정과 백엔드
   이미지, 프런트 #5의 승인된 SHA를 사용합니다. 초기에는
   `STAGING_ENABLE_SPOT_TESTNET_ORDERS=false`와
   `STAGING_ENABLE_TRADING_SCHEDULER=false`를 유지합니다.
2. Tailscale 검증 주소에서 `/api/health`가
   `status=ok`, `dry_run=true`, `binance_testnet=true`를 반환하고
   `/api/health/db`가 `status=ok`인지 확인합니다.
3. 검증 관리자 계정으로 가입·로그인합니다. 새로고침 후 로그인 상태가
   유지되고 `GET /api/me`, 세션 갱신 `POST /api/auth/refresh`가 성공해야
   합니다. `GET /api/backtests/data/status`가 200이고 빈 검증 DB에서는
   캔들 0건이 정상입니다. 계좌 현황의 `/api/trading/status`에서
   `scheduler_enabled=false`, `spot_testnet_orders_enabled=false`를 확인합니다.

## 2. 키 등록과 소액 주문 시나리오

1. 재준님이 **계정 설정 → API Key 등록**에서 현물 테스트넷 전용 키를 직접
   입력합니다. 키나 자격증명을 채팅·명령·기록에 붙여넣지 않습니다.
   같은 화면에서 새 검증 계좌를 만듭니다. 계정과 사용자의 모의 실행
   설정은 유지합니다. **계좌 현황**의 BTC·USDT 잔고가 테스트넷과 일치하고,
   체결은 비어 있으며 ‘봇 보호주문 없음’이 표시되어야 합니다.
   열린 BTCUSDT 보호주문이 없는 전용 테스트넷 계정을 사용합니다.
2. 검증 폴더의 `.env.staging`에서
   `STAGING_ENABLE_SPOT_TESTNET_ORDERS=true`로 바꿉니다.
   `STAGING_SPOT_TESTNET_MAX_ORDER_USDT` 기본값은 12이고 상한은 20입니다.
   Binance의 최소 주문 금액과 보호주문 최소 수량을 먼저 확인합니다.
   설치 문서에서 정의한 `staging_compose` 함수로 검증 API만 올립니다.

   ```bash
   staging_compose up -d --no-deps --wait staging-api
   ```

   건강 상태는 그대로 `dry_run=true`입니다. 로그인한 관리자의 계좌 현황
   API에서 `spot_testnet_orders_enabled=true`, `is_live_trading=false`를
   확인합니다. 본 검증에서는 스케줄러를 기본 꺼짐으로 두고 주문 진행을
   수동 확인합니다.
3. **소액 매수와 OCO 등록.** 검증 주소의 로그인된 브라우저 개발자 도구에서
   아래 명령의 `123`을 새 검증 계좌 ID로 바꿔 한 번 실행합니다.
   계좌 ID는 계좌 현황의 요청 경로나 `/api/trading/accounts`에서 확인합니다.
   코드는 현재 페이지의 세션을 사용합니다.

   ```javascript
   const spotCheck = async (id, action) => {
     if (!location.hostname.startsWith("trade-staging.") || !location.hostname.endsWith(".ts.net")) {
       throw new Error("검증 서버 주소에서만 실행하세요.");
     }
     const response = await fetch(`/api/trading/accounts/${id}/spot-testnet-check?action=${action}`, {
       method: "POST",
     });
     console.log(response.status, await response.json());
   };
   await spotCheck(123, "entry");
   ```

   정상 응답은 `status=protected`입니다. 계좌 현황에서 BUY 체결, 수량·수수료,
   봇 OCO의 상태·수량·가격이 보이고 테스트넷의 보호주문과 일치해야 합니다.
   BUY는 12 USDT 기본 예산의 `quoteOrderQty` 시장가 요청 한 건입니다.
   동일 계좌에서 같은 매수를 재시도하면 `blocked` 또는 동시 실행 중
   `busy`이며 주문·원장 건수가 증가하지 않아야 합니다. 응답이 모호하면
   새 계좌로 재시도하기 전에 테스트넷 주문 화면과 원장을 먼저 대조합니다.
4. **재시작 복구.** OCO가 열린 상태에서 계좌 현황의 OCO ID와 주문 수를
   기록합니다. 자동 복구를 확인할 때만 `STAGING_ENABLE_TRADING_SCHEDULER=true`
   를 검증 파일에 지정한 뒤 설치 문서의 API 교체 명령을 실행합니다.
   필요하면 같은 검증 스택에서 `staging_compose restart staging-api`를
   실행합니다. API가 정상으로 돌아오고, 보호주문이 여전히 열려 있다면
   이전 OCO ID가 유지되어야 합니다. 재시작 중 체결됐다면 해당 매도 원장이
   한 번만 기록되어야 합니다. 어느 경우에도 새 BUY가 없어야 합니다.
   스케줄러는 검증 중 상태 정합성 확인과 복구만 수행합니다.
5. **익절·손절.** 가격이 한쪽 조건에 도달하면 동일 콘솔에서
   `await spotCheck(123, "advance");`를 실행하거나 검증 스케줄러의 다음
   확인 주기를 기다립니다. 매도 원장 기록, 반대 주문 종료, 중복 매도 없음이
   통과 기준입니다. 익절과 손절을 각각 보려면 앞선 계좌가 종료된 후 새 계좌로
   반복합니다. 가격이 어느 쪽에 도달할지와 시간은 보장되지 않으므로 실제로
   관찰하지 못한 쪽은 미검증으로 남깁니다.
6. **OCO 실패와 비상 매도.** 이전 주문이 모두 종료된 뒤 또 다른 새 검증
   계좌에서 `await spotCheck(새_검증_계좌_ID, "reject_oco");`를 실행합니다.
   검증용으로 동일 손절·익절 가격을 만들어 앱 내부에서 보호주문을 거절시키므로
   거래소에 잘못된 OCO를 제출하지 않습니다. 정상 결과는 BUY 및 봇 보유분
   SELL 기록, OCO 미등록, 계좌 중지, 계좌 현황의 중지 사유 표시입니다.
   매도량은 원장상 봇 귀속 수량과 매도 가능 잔고 이하여야 합니다.
   실제 거래소 OCO 거절과 응답 불명 상태는 아래 모의 테스트로 구분해 확인합니다.
7. **종료.** 열린 주문·봇 보유분을 테스트넷과 대조합니다. 남은 주문이나
   잔고가 있으면 테스트넷 화면에서 상태를 확인해 정리합니다.
   `STAGING_ENABLE_SPOT_TESTNET_ORDERS=false`와
   `STAGING_ENABLE_TRADING_SCHEDULER=false`로 되돌려 검증 API를 다시 올립니다.
   `/api/trading/status`의 두 플래그가 모두 꺼져 있어야 합니다.

매수 상한은 거래소에 전달되는 USDT 금액입니다. 보호·비상 매도는 시세가
상승해도 검증 매수분을 정리할 수 있도록 USDT 상한 대신 확인된 봇 BTC
수량으로 제한합니다. 시장가 체결과 수수료는 거래소 기준을 따릅니다.

키 없이 주문 가드·체결 원장·익절/손절·OCO 실패·재시도·복구를 다시
확인하려면 다음 모의 테스트를 실행합니다.

```bash
python -m pytest -q tests/test_spot_testnet_order_guard.py tests/test_spot_lifecycle.py tests/test_spot_service.py tests/test_spot_recovery_service.py tests/test_scheduler_guard.py
```

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
| `ENABLE_SPOT_TESTNET_ORDERS` | 운영에는 누락 또는 꺼짐. 운영 호스트에서 켜진 상태라면 앱은 기동을 거부합니다. |
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
