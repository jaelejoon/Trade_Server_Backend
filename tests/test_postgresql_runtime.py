import os
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text


@pytest.mark.skipif(not os.environ.get('TEST_DATABASE_URL'), reason='별도 PostgreSQL TEST_DATABASE_URL이 필요합니다. 운영 DB를 사용하지 마세요.')
def test_runtime_state_round_trip_and_migration(monkeypatch):
    from app.core.jwt import get_current_user
    from app.db.base import Base
    from app.db.database import engine
    from app.db.migrations import ensure_runtime_schema
    from app.models import BotSignalState, User, BinanceApiKey, TradingAccount
    from sqlalchemy.orm import Session
    from app.models.trading_strategy import TradingStrategy  # noqa: F401
    from app.services import bot_service as bot
    from app.models.spot_fill import SpotFill
    from app.services.spot_recovery_service import record_order_fills

    # 반드시 비어 있는 전용 테스트 DB를 지정한다.
    with engine.connect() as connection:
        count = connection.execute(text("SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE'")).scalar()
        assert count == 0, '빈 전용 테스트 DB에서만 실행할 수 있습니다.'
    try:
        first_app = runpy.run_module('app.main')['app']
        with TestClient(first_app) as client:
            assert client.get('/').status_code == 200
        with engine.begin() as connection:
            connection.execute(User.__table__.insert().values(
                id=9001, email='backtest@example.invalid', password_hash='test-only',
            ))
            run_id = connection.execute(text("""
                INSERT INTO backtest_runs (user_id, status, request)
                VALUES (9001, 'succeeded', '{}') RETURNING id
            """)).scalar_one()
            relation_ids = text("""
                SELECT 'backtest_runs'::regclass::oid,
                       'backtest_runs_user_history_idx'::regclass::oid
            """)
            original_ids = connection.execute(relation_ids).one()
            connection.execute(text("DROP INDEX backtest_runs_active_idx"))
        restarted_app = runpy.run_module('app.main')['app']
        ensure_runtime_schema()
        with engine.begin() as connection:
            assert connection.execute(relation_ids).one() == original_ids
            assert connection.execute(text("SELECT 'backtest_runs_active_idx'::regclass::oid")).scalar_one()
            migration = Path("migrations/001_backtest_runs.sql").read_text()
            connection.exec_driver_sql(migration)
            connection.exec_driver_sql(migration)
            assert connection.execute(
                text("SELECT user_id, status FROM backtest_runs WHERE id = :id"), {"id": run_id},
            ).one() == (9001, 'succeeded')
            connection.execute(text("""
                INSERT INTO spot_candles_1m (user_id, symbol, open_time, open, high, low, close, volume)
                SELECT 9001, 'BTCUSDT', candle_time, 100, 100, 100, 100, 1
                FROM generate_series(timestamp '2026-01-01' - interval '800 hours',
                                     timestamp '2026-01-01 00:01', interval '1 minute') AS candle_time
            """))
        restarted_app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=9001)
        with TestClient(restarted_app) as client:
            assert client.get('/').status_code == 200
            assert client.get('/backtests/runs').json()['items'][0]['id'] == run_id
            response = client.post('/backtests/runs', json={
                'strategy_id': 'V57_LONG_SAFE_PUSH',
                'start': '2026-01-01T00:00:00Z',
                'end': '2026-01-01T00:02:00Z',
                'initial_equity_usdt': 1000,
                'funding': 'disabled',
            })
            assert response.status_code == 202
            created_id = response.json()['id']
            detail = client.get(f'/backtests/runs/{created_id}').json()
            assert detail['status'] == 'succeeded'
            assert client.get('/backtests/runs').json()['items'][0]['id'] == created_id
        with Session(engine) as session:
            user = User(email='isolated-test@example.invalid', password_hash='test-only')
            session.add(user)
            session.flush()
            key = BinanceApiKey(user_id=user.id, api_key_encrypted='unused-test', secret_key_encrypted='unused-test')
            session.add(key)
            session.flush()
            account = TradingAccount(user_id=user.id, api_key_id=key.id, dry_run=True)
            session.add(account)
            session.flush()
            state = BotSignalState(trading_account_id=account.id, runtime_state={'position': {'stop_price': 99.}, 'guard': {'peak': 1000.}})
            session.add(state)
            session.commit()
            state_id = state.id
        with Session(engine) as restarted:
            restored = restarted.get(BotSignalState, state_id)
            assert restored.runtime_state['position']['stop_price'] == 99.
            assert restored.runtime_state['guard']['peak'] == 1000.
            account = restarted.get(TradingAccount, restored.trading_account_id)
            assert account.market_type == "SPOT"
            assert bot.execute_bot_tick(restarted, account)["status"] == "paused"
            trade = {"orderId": 5, "id": 6, "isBuyer": True, "qty": "0.02",
                     "commission": "0.001", "commissionAsset": "BTC"}
            record_order_fills(restarted, account.id, 5, [trade])
            record_order_fills(restarted, account.id, 5, [trade])
            restarted.commit()
            fill = restarted.query(SpotFill).filter_by(trading_account_id=account.id).one()
            assert fill.trade_id == 6 and str(fill.quantity.normalize()) == "0.02"
        with engine.connect() as connection:
            name = connection.execute(text("SELECT data_type FROM information_schema.columns WHERE table_name='bot_signal_states' AND column_name='runtime_state'")).scalar()
            assert name == 'jsonb'
            payload = connection.execute(text("SELECT CAST(:payload AS jsonb)"), {'payload': '{"position":{"entry_price":100,"stop_price":99},"guard":{"peak":1000}}'}).scalar()
            assert payload['position']['stop_price'] == 99
            assert connection.execute(text('SELECT pg_try_advisory_lock(571124, 1)')).scalar()
            with engine.connect() as competing:
                assert not competing.execute(text('SELECT pg_try_advisory_lock(571124, 1)')).scalar()
            connection.execute(text('SELECT pg_advisory_unlock(571124, 1)'))
    finally:
        Base.metadata.drop_all(engine)
