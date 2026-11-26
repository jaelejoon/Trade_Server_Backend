import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.routers import backtest_data

PREFIX = "/backtests/data"
START = datetime(2025, 1, 1)
MINUTE = timedelta(minutes=1)


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(backtest_data.router, prefix="/backtests")
    monkeypatch.setattr(backtest_data, "_closed_before", lambda: START + 9 * timedelta(hours=1))

    def reject_query(*args, **kwargs):
        pytest.fail("이 요청은 캔들 데이터를 조회하면 안 됩니다.")

    app.dependency_overrides[backtest_data.get_db] = lambda: SimpleNamespace(execute=reject_query, query=reject_query)
    with TestClient(app) as http:
        yield http, app


@pytest.mark.parametrize("path", ["status", "integrity", "candles"])
def test_requires_login(client, path):
    http, _ = client
    result = http.get(f"{PREFIX}/{path}", params={
        "start": "2025-01-01T00:00:00Z", "end": "2025-01-02T00:00:00Z",
    })
    assert result.status_code == 401


@pytest.mark.parametrize("invalid", [
    {"start": "2025-01-01T00:00:00"},
    {"end": "2024-12-31T23:00:00Z"},
    {"interval": "5m"},
    {"limit": 1001},
    {"cursor": "2024-12-31T23:59:00Z"},
    {"cursor": "2025-01-01T00:01:30Z"},
])
def test_rejects_invalid_query(client, invalid):
    http, app = client
    app.dependency_overrides[backtest_data.get_current_user] = lambda: SimpleNamespace(id=1)
    assert http.get(f"{PREFIX}/candles", params={
        "start": "2025-01-01T00:00:00Z", "end": "2025-01-02T00:00:00Z", **invalid,
    }).status_code == 422


@pytest.fixture
def postgres_client(client):
    database_url = os.getenv("BACKTEST_TEST_DATABASE_URL") or os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL 또는 BACKTEST_TEST_DATABASE_URL이 설정된 PostgreSQL 테스트 환경이 필요합니다.")
    engine = create_engine(database_url)
    http, app = client
    try:
        with engine.connect() as connection:
            connection.execute(text("""
                CREATE TEMP TABLE candles_1m (
                    user_id integer, symbol text, open_time timestamp,
                    open float8, high float8, low float8, close float8, volume float8
                )
            """))
            connection.execute(text("""
                INSERT INTO candles_1m VALUES
                    (:user_id, :symbol, :open_time, :price, :price + 2,
                     :price - 2, :price + 1, 2)
            """), [
                {"user_id": user_id, "symbol": symbol, "open_time": START + minute * MINUTE,
                 "price": 1000 + minute}
                for user_id, symbol, minute in (
                    [(1, "BTCUSDT", n) for n in range(541) if n not in (245, 246)]
                    + [(1, "BTCUSDT", 300), (1, "BTCUSDT", 300.5),
                       (2, "BTCUSDT", 245), (1, "ETHUSDT", 246)]
                )
            ])
            connection.commit()
            connection.execute(text("SET TRANSACTION READ ONLY"))

            def get_test_db():
                with Session(bind=connection) as db:
                    yield db

            app.dependency_overrides[backtest_data.get_db] = get_test_db
            app.dependency_overrides[backtest_data.get_current_user] = lambda: SimpleNamespace(id=1)
            yield http, app
    finally:
        engine.dispose()


def test_reports_coverage_and_integrity(postgres_client):
    http, _ = postgres_client
    status = http.get(f"{PREFIX}/status").json()
    assert status == {
        "symbol": "BTCUSDT", "interval": "1m",
        "start": "2025-01-01T00:00:00+00:00", "end": "2025-01-01T08:59:00+00:00",
        "candle_count": 538, "gap_count": 1,
    }
    assert http.get(f"{PREFIX}/integrity").json() == {
        **status, "missing_minutes": 2, "duplicate_timestamps": 1,
        "duplicate_rows": 1, "misaligned_rows": 1,
    }


@pytest.mark.parametrize("interval,minutes", [("1m", 1), ("15m", 15), ("1h", 60), ("4h", 240)])
def test_aggregates_and_paginates(postgres_client, interval, minutes):
    http, _ = postgres_client
    params = {
        "start": "2025-01-01T09:00:00+09:00", "end": "2025-01-02T00:00:00Z",
        "interval": interval, "limit": 1,
    }
    first = http.get(f"{PREFIX}/candles", params=params).json()
    assert first["items"] == [{
        "open_time": START.replace(tzinfo=timezone.utc).isoformat(),
        "open": 1000, "high": 1001 + minutes, "low": 998,
        "close": 1000 + minutes, "volume": 2 * minutes,
    }]
    if interval == "4h":
        assert first["next_cursor"] is None
    else:
        next_page = http.get(f"{PREFIX}/candles", params={**params, "cursor": first["next_cursor"]}).json()
        assert datetime.fromisoformat(next_page["items"][0]["open_time"]).replace(tzinfo=None) == (
            START + minutes * MINUTE
        )


def test_excludes_partial_and_corrupt_buckets(postgres_client):
    http, _ = postgres_client
    result = http.get(f"{PREFIX}/candles", params={
        "start": "2025-01-01T01:00:01Z", "end": "2025-01-01T08:59:59Z", "interval": "1h",
    }).json()
    assert [candle["open_time"][11:16] for candle in result["items"]] == [
        "02:00", "03:00", "06:00", "07:00",
    ]
    assert result["next_cursor"] is None


def test_empty_account(postgres_client):
    http, app = postgres_client
    app.dependency_overrides[backtest_data.get_current_user] = lambda: SimpleNamespace(id=3)
    result = http.get(f"{PREFIX}/status").json()
    assert (result["start"], result["end"], result["candle_count"], result["gap_count"]) == (None, None, 0, 0)
