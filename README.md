# Trade Server Stability V2

Python + FastAPI 기반 다중 사용자 Binance Futures 자동매매 서버입니다.

## 이번 버전에서 추가된 안정성 보강

- 사용자별 1분봉 DB 누적 저장
- DB 1분봉 기반 V57 리샘플링 신호 계산
- tick 시작 시 포지션/미체결 주문 reconcile 수행
- 포지션이 없는데 남은 STOP_MARKET / TAKE_PROFIT_MARKET 주문이 있으면 자동 취소
- Binance Futures exchangeInfo 기반 수량 stepSize / 가격 tickSize 정규화
- 최소 주문 금액 minNotional 검사
- `/trading/accounts/{id}/reconcile` 수동 정리 API 추가

## 실행 방법

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python run.py
```

브라우저에서 확인:

```text
http://127.0.0.1:8000/docs
```

## 주요 흐름

```text
회원가입/로그인
→ Binance API Key 저장
→ 자동매매 계정 생성
→ 1분봉 DB 누적
→ V57 전략 계산
→ 포지션/미체결 주문 reconcile
→ DRY_RUN 또는 실주문
→ 브래킷 주문 생성
→ 다음 tick에서 포지션 종료 감지 및 남은 반대 주문 정리
```

## 중요

- 기본값은 `DRY_RUN=true`입니다.
- 실계정 전환 전 Testnet에서 충분히 검증하세요.
- `.env` 안의 `ENCRYPTION_KEY`는 개발용입니다. 운영 전 반드시 새 키로 교체하세요.
- V57 계산식은 `app/strategy/strategy_v57.py`에 유지되어 있습니다.
- Binance Futures의 STOP_MARKET / TAKE_PROFIT_MARKET closePosition 주문은 OCO가 아니므로, 이번 버전은 tick마다 남은 반대 주문을 정리합니다.
