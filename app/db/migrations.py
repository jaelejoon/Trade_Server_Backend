from sqlalchemy import text
from sqlalchemy.schema import CreateIndex

from app.db.database import engine
from app.models.backtest_run import backtest_runs


def ensure_runtime_schema() -> None:
    """기존 PostgreSQL DB에 새 컬럼/기본 설정을 보강한다."""
    with engine.begin() as conn:
        # create_all skips indexes when the table already exists.
        for index in sorted(backtest_runs.indexes, key=lambda index: index.name):
            conn.execute(CreateIndex(index, if_not_exists=True))

        conn.execute(text("ALTER TABLE bot_signal_states ADD COLUMN IF NOT EXISTS runtime_state JSONB NOT NULL DEFAULT '{}'::jsonb"))
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS dry_run BOOLEAN NOT NULL DEFAULT TRUE"))
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN NOT NULL DEFAULT FALSE"))
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_prime_admin BOOLEAN NOT NULL DEFAULT FALSE"))
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE"))
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS enable_live_trading BOOLEAN NOT NULL DEFAULT FALSE"))
        conn.execute(text("ALTER TABLE trading_accounts ADD COLUMN IF NOT EXISTS dry_run BOOLEAN NOT NULL DEFAULT TRUE"))

        conn.execute(text("""
            UPDATE users
            SET is_admin = TRUE
            WHERE id = (
                SELECT id
                FROM users
                ORDER BY id ASC
                LIMIT 1
            )
            AND NOT EXISTS (
                SELECT 1
                FROM users
                WHERE is_admin = TRUE
            )
        """))



    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE users
            SET is_prime_admin = TRUE,
                is_admin = TRUE,
                is_active = TRUE
            WHERE id = (
                SELECT id
                FROM users
                ORDER BY id ASC
                LIMIT 1
            )
            AND NOT EXISTS (
                SELECT 1
                FROM users
                WHERE is_prime_admin = TRUE
            )
        """))
