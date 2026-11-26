from pathlib import Path
import os
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.routers import backtest_runs as api
from app.routers.backtests import router
from app.strategy.strategy_v57 import prepare_closed_candles
from app.services.backtest_engine import (
    BacktestInputError, Signal, replay, v57_signals, validate_candles,
)


def candles(opens, closes):
    return pd.DataFrame(
        {"open": opens, "high": np.maximum(opens, closes), "low": np.minimum(opens, closes),
         "close": closes, "volume": 1.0},
        index=pd.date_range("2026-01-01", periods=len(opens), freq="min"),
    )


def test_next_open_and_round_trip_costs():
    bars = candles([80, 100, 110], [90, 102, 110])
    signals = {bars.index[1]: Signal(1, 50, 10, 1, 0.5, 500)}
    result = replay(bars, signals, 1000, 1, 100, 0, {})
    trade, = result["trades"]
    metrics = result["metrics"]
    assert trade["entry_time"] == "2026-01-01T00:01:00Z"
    assert trade["entry_price"] == 100
    assert trade["exit_price"] == 110
    assert trade["fees_usdt"] == 10.5
    assert trade["net_pnl_usdt"] == 39.5
    assert metrics["final_equity_usdt"] == 1039.5
    assert metrics["net_return_pct"] == pytest.approx(3.95)
    assert metrics["mdd_pct"] == 0
    assert metrics["win_rate_pct"] == 100
    assert metrics["profit_factor"] is None
    assert metrics["liquidation_occurred"] is False
    assert result["equity_curve"][-1] == ["2026-01-01T00:03:00Z", 1039.5]


def test_short_funding_and_stop_before_target():
    bars = candles([100, 100], [100, 100])
    bars.iloc[1, bars.columns.get_loc("high")] = 120
    bars.iloc[1, bars.columns.get_loc("low")] = 80
    result = replay(bars, {bars.index[0]: Signal(-1, 10, 1, 1, 0.5, 500)},
                    1000, 1, 0, 0, {bars.index[1]: (100, 0.01)})
    trade, = result["trades"]
    assert trade["exit_reason"] == "stop_loss"
    assert trade["funding_cost_usdt"] == -5
    assert trade["net_pnl_usdt"] == -45
    assert result["metrics"]["mdd_pct"] == pytest.approx(4.5)
    assert result["metrics"]["max_consecutive_losses"] == 1
    assert result["metrics"]["profit_factor"] == 0


def test_slippage_and_gap_liquidation():
    bars = candles([100, 40], [100, 40])
    signals = {bars.index[0]: Signal(1, 80, 10, 1, 0.5, 500)}
    result = replay(bars, signals, 1000, 2, 0, 50, {})
    trade, = result["trades"]
    assert trade["entry_price"] == pytest.approx(100.5)
    assert trade["exit_price"] == pytest.approx(39.8)
    assert trade["exit_reason"] == "liquidation"
    assert result["metrics"]["liquidation_occurred"] is True


def test_v57_signal_prefix_is_invariant():
    rng = np.random.default_rng(17)
    hours = 24 * 55
    hourly_log_prices = np.r_[0, np.cumsum(rng.normal(0.0008, 0.012, hours))]
    prices = 25_000 * np.exp(np.interp(np.arange(hours * 60 + 1), np.arange(hours + 1) * 60, hourly_log_prices))
    bars = candles(prices[:-1], prices[1:])
    original = v57_signals(bars)
    boundary = bars.index[-1] + pd.Timedelta(minutes=1)
    assert any(time < boundary for time in original)
    changed = bars.copy()
    changed.iloc[-1, :4] *= 1.25
    assert {time: value for time, value in v57_signals(changed).items() if time < boundary} == {
        time: value for time, value in original.items() if time < boundary
    }
    for cutoff in (1, 75, 137, 233):
        prefix = bars.iloc[:-cutoff]
        closed_through = prefix.index[-1] + pd.Timedelta(minutes=1)
        assert v57_signals(prefix) == {
            time: value for time, value in original.items() if time <= closed_through
        }

    # A previously closed 4h/1h candle must remain visible even while the
    # following higher-timeframe candle is still incomplete.
    full_hours, full_quarters = prepare_closed_candles(bars)
    prefix_hours, prefix_quarters = prepare_closed_candles(bars.iloc[:-1])
    pd.testing.assert_frame_equal(prefix_hours, full_hours.loc[prefix_hours.index])
    pd.testing.assert_frame_equal(prefix_quarters, full_quarters.loc[prefix_quarters.index])


@pytest.mark.parametrize("row_indices", [[0, 2], [0, 1, 1, 2]])
def test_gap_and_duplicate_rejected(row_indices):
    bars = candles([100, 100, 100], [100, 100, 100])
    with pytest.raises(BacktestInputError, match="누락"):
        validate_candles(bars.iloc[row_indices], bars.index[0],
                         bars.index[-1] + pd.Timedelta(minutes=1))


REQUEST = {
    "strategy_id": "V57_LONG_SAFE_PUSH", "start": "2026-01-01T09:00:00+09:00",
    "end": "2026-01-01T09:02:00+09:00", "initial_equity_usdt": 1000,
}


def test_spot_run_defaults_to_zero_funding():
    body = api.RunRequest.model_validate(REQUEST)
    assert body.funding == "disabled"
    assert body.leverage == 1


@pytest.mark.parametrize("changes", [
    {"symbol": "ETHUSDT"}, {"funding": "historical"}, {"leverage": 2}, {"leverage": True}, {"strategy_version": "2"},
    {"start": 1767225600}, {"start": "2026-01-01T00:00:00"}, {"end": "2026-03-01T00:00:00Z"},
])
def test_request_constraints(changes):
    response_app = FastAPI()
    response_app.include_router(router)
    response_app.dependency_overrides[api.get_current_user] = lambda: SimpleNamespace(id=1)
    with TestClient(response_app) as client:
        assert client.post("/backtests/runs", json={**REQUEST, **changes}).status_code == 422


def test_run_lifecycle_postgresql(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("isolated PostgreSQL required")
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            # The test connection owns and drops its own session-local tables.
            with conn.begin():
                conn.execute(text("CREATE TEMP TABLE users (id integer PRIMARY KEY)"))
                conn.execute(text("INSERT INTO users VALUES (1), (2)"))
                migration = Path("migrations/001_backtest_runs.sql").read_text()
                conn.exec_driver_sql(migration.replace("CREATE TABLE", "CREATE TEMP TABLE", 1))
                conn.execute(text("""CREATE TEMP TABLE spot_candles_1m (
                    user_id integer, symbol text, open_time timestamp,
                    open float, high float, low float, close float, volume float
                )"""))
                conn.execute(text("""INSERT INTO spot_candles_1m
                    SELECT 1, 'BTCUSDT', time, 100, 100, 100, 100, 1
                    FROM generate_series(timestamp '2026-01-01' - interval '800 hours',
                                         timestamp '2026-01-01 00:01', interval '1 minute') AS time
                """))
            monkeypatch.setattr(api, "SessionLocal", lambda: Session(bind=conn))
            application = FastAPI()
            application.include_router(router)
            application.dependency_overrides[api.get_db] = lambda: Session(bind=conn)
            with TestClient(application) as client:
                assert client.get("/backtests/runs").status_code == 401
                application.dependency_overrides[api.get_current_user] = lambda: SimpleNamespace(id=1)
                response = client.post("/backtests/runs", json={**REQUEST, "funding": "disabled"})
                assert response.status_code == 202
                run_id = response.json()["id"]
                detail = client.get(f"/backtests/runs/{run_id}?limit=1").json()
                assert detail["status"] == "succeeded"
                assert detail["result"]["equity_curve"]["next_offset"] == 1
                assert detail["result"]["metrics"]["final_equity_usdt"] == 1000
                assert detail["result"]["funding"]["label"] == "미반영"
                assert client.get("/backtests/runs").json()["items"][0]["id"] == run_id
                application.dependency_overrides[api.get_current_user] = lambda: SimpleNamespace(id=2)
                assert client.get(f"/backtests/runs/{run_id}").status_code == 404
                assert client.get("/backtests/runs").json()["items"] == []
                gap = client.post("/backtests/runs", json={**REQUEST, "funding": "disabled"}).json()
                assert client.get(f"/backtests/runs/{gap['id']}").json()["status"] == "failed"
    finally:
        engine.dispose()
