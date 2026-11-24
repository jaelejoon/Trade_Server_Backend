import os
from unittest.mock import Mock

import pytest
from sqlalchemy import text


@pytest.mark.skipif(not os.environ.get('TEST_DATABASE_URL'), reason='별도 PostgreSQL TEST_DATABASE_URL이 필요합니다. 운영 DB를 사용하지 마세요.')
def test_runtime_state_round_trip_and_migration(monkeypatch):
    from app.db.base import Base
    from app.db.database import engine
    from app.db.migrations import ensure_runtime_schema
    from app.models import BotSignalState, User, BinanceApiKey, TradingAccount
    from sqlalchemy.orm import Session
    from app.models.trading_strategy import TradingStrategy  # noqa: F401
    from app.services import bot_service as bot

    # 반드시 비어 있는 전용 테스트 DB를 지정한다.
    with engine.connect() as connection:
        count = connection.execute(text("SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE'")).scalar()
        assert count == 0, '빈 전용 테스트 DB에서만 실행할 수 있습니다.'
    try:
        Base.metadata.create_all(engine)
        ensure_runtime_schema()
        ensure_runtime_schema()
        with Session(engine) as session:
            user = User(email='isolated-test@example.invalid', password_hash='test-only')
            session.add(user)
            session.flush()
            key = BinanceApiKey(user_id=user.id, api_key_encrypted='unused-test', secret_key_encrypted='unused-test')
            session.add(key)
            session.flush()
            account = TradingAccount(user_id=user.id, api_key_id=key.id, dry_run=True, leverage=5)
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
            assert account.leverage == 5
            monkeypatch.setattr(bot, 'get_api_key_owned_by_user', Mock())
            monkeypatch.setattr(bot, 'get_decrypted_api_keys', lambda _: ('k', 's'))
            monkeypatch.setattr(bot, 'reconcile_position_and_open_orders', lambda **_: {'active_position': None})
            monkeypatch.setattr(bot.BinanceCandleSyncService, 'sync_recent_1m_candles', Mock())
            monkeypatch.setattr(bot, 'manage_position', lambda *_: ({}, None))
            monkeypatch.setattr(bot, '_save_latest_signal_state_safe', Mock())
            submit = Mock()
            monkeypatch.setattr(bot, 'place_market_with_bracket', submit)
            result = bot._execute_bot_tick(restarted, account)
            assert result['status'] == 'blocked'
            assert '거래 계정 설정' in result['message']
            submit.assert_not_called()
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
