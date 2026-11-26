CREATE TABLE IF NOT EXISTS backtest_runs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
    request JSONB NOT NULL,
    result JSONB,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS backtest_runs_user_history_idx ON backtest_runs (user_id, id DESC);
CREATE INDEX IF NOT EXISTS backtest_runs_active_idx ON backtest_runs (status, updated_at)
    WHERE status IN ('queued', 'running');
