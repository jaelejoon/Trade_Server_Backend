#!/usr/bin/env bash
set +x
set -u

container=trade-backtest-api
env_file=/home/joon/trade-server-backend/.env
printf '%s\n' '=== SPOT PREDEPLOY BEGIN ==='
trap 'printf "%s\n" "=== SPOT PREDEPLOY END ==="' EXIT

if ! info=$(docker inspect --type container --format '{{.State.Running}} {{.Config.Image}} {{.Image}}' "$container" 2>/dev/null); then
    printf '%s\n' 'API 컨테이너: 확인 불가'
    exit 1
fi
read -r running image image_id <<< "$info"
if [[ "$running" != true ]]; then
    printf '%s\n' 'API 컨테이너: 실행 중 아님'
    exit 1
fi
printf 'API 컨테이너: %s\n' "$container"
if [[ "$image_id" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    printf '이미지 ID: %s\n' "$image_id"
fi
if [[ "$image" =~ ^trade-backend-deploy:([0-9a-f]{40})$ ]]; then
    printf '이미지 Git SHA 태그: %s\n' "${BASH_REMATCH[1]}"
fi

input=/dev/null
env_status=unavailable
if [[ -r "$env_file" ]]; then
    input=$env_file
    env_status=readable
fi

if ! docker exec -i "$container" python -I -B -u -c '
import os
import sys

from cryptography.fernet import Fernet
from dotenv import dotenv_values
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url


SPOT_PRODUCTION = "https://api.binance.com"


def is_postgres(value):
    return value.startswith(("postgresql://", "postgresql+psycopg2://")) and bool(make_url(value).database)


def is_fernet(value):
    Fernet(value.encode())
    return True


def report_flags(values, heading):
    print(heading)
    rules = {
        "JWT_SECRET_KEY": lambda v: len(v.strip().encode()) >= 32,
        "ENCRYPTION_KEY": is_fernet,
        "DATABASE_URL": is_postgres,
        "ALLOWED_ORIGINS": lambda v: "*" not in {part.strip() for part in v.split(",")},
        "BINANCE_TESTNET": lambda v: v.lower() == "false",
        "BINANCE_SPOT_BASE_URL": lambda v: v.rstrip("/") == SPOT_PRODUCTION,
        "DRY_RUN": lambda v: v.lower() == "true",
        "ENABLE_LIVE_TRADING": lambda v: v.lower() == "false",
        "ENABLE_TRADING_SCHEDULER": lambda v: v.lower() == "false",
        "ENVIRONMENT": None,
        "REQUIRE_HTTPS_AUTH": lambda v: v.lower() == "true",
        "TRUST_PROXY_HEADERS": lambda v: v.lower() in {"true", "false"},
        "AUTH_COOKIE_SECURE": lambda v: v.lower() == "true",
        "AUTH_COOKIE_SAMESITE": lambda v: v.lower() in {"lax", "strict", "none"},
        "BINANCE_FUTURES_BASE_URL": None,
    }
    for name, check in rules.items():
        if name not in values:
            print(f"{name}: 없음")
            continue
        try:
            valid = check(values[name] or "") if check else None
        except Exception:
            valid = False
        detail = "" if valid is None else " · 적합" if valid else " · 부적합"
        print(f"{name}: 있음{detail}")


def report_db():
    url = os.environ.get("DATABASE_URL", "")
    try:
        if not is_postgres(url):
            return False
        engine = create_engine(url, isolation_level="AUTOCOMMIT", connect_args={
            "connect_timeout": 5,
            "options": "-c default_transaction_read_only=on -c statement_timeout=20000",
        })
        with engine.connect() as conn:
            if conn.exec_driver_sql("SELECT current_setting(%s)", ("transaction_read_only",)).scalar_one() != "on":
                return False

            all_read = True

            def report(label, query, params=None, render=str):
                nonlocal all_read
                try:
                    detail = render(*conn.exec_driver_sql(query, params).one())
                except Exception:
                    detail = "확인 불가"
                    all_read = False
                print(f"{label}: {detail}")

            def presence(found):
                return "있음" if found else "없음"

            report("사용자 수", "SELECT count(*) FROM users")
            report("관리자 수", "SELECT count(*) FROM users WHERE is_admin IS TRUE")
            report("최고관리자 수", "SELECT count(*) FROM users WHERE is_prime_admin IS TRUE")
            report("기존 비현물 계정 수", "SELECT count(*) FROM trading_accounts WHERE market_type IS DISTINCT FROM %s", ("SPOT",))
            report("실행 표시가 켜진 계정 수", "SELECT count(*) FROM trading_accounts WHERE is_running IS TRUE")
            report("실행 표시가 켜진 현물 계정 수", "SELECT count(*) FROM trading_accounts WHERE is_running IS TRUE AND market_type = %s", ("SPOT",))
            tables = (
                "spot_candles_1m", "spot_fills", "backtest_runs", "candles_1m",
                "refresh_tokens", "login_attempts", "latest_signal_states", "app_settings",
                "candle_import_jobs", "trading_strategies", "user_strategy_settings",
                "strategy_setting_histories", "strategy_backtest_runs",
            )
            for table in tables:
                report(table, "SELECT to_regclass(%s) IS NOT NULL", (table,), presence)

            for table, column in (
                ("bot_signal_states", "runtime_state"),
                ("users", "dry_run"),
                ("users", "is_admin"),
                ("users", "is_prime_admin"),
                ("users", "is_active"),
                ("users", "enable_live_trading"),
                ("trading_accounts", "dry_run"),
            ):
                report(f"{table}.{column}", """
                    SELECT EXISTS (
                        SELECT 1 FROM pg_catalog.pg_attribute
                        WHERE attrelid = to_regclass(%s)
                          AND attname = %s AND attnum > 0 AND NOT attisdropped
                    )
                """, (table, column), presence)

            if conn.exec_driver_sql("SELECT to_regclass(%s) IS NOT NULL", ("spot_candles_1m",)).scalar_one():
                report("현물 1분봉 개수 / 최소 / 최대", """
                    SELECT count(*), min(open_time), max(open_time) FROM spot_candles_1m
                """, render=lambda count, first, last: " / ".join(
                    (str(count), first.isoformat() if first else "없음", last.isoformat() if last else "없음")
                ))
            return all_read
    except Exception:
        return False


env_ok = False
if sys.argv[1] == "readable":
    try:
        report_flags(dotenv_values(stream=sys.stdin, interpolate=False), "배포용 .env · 운영 현물 기준")
        env_ok = True
    except Exception:
        pass
if not env_ok:
    print("배포용 .env: 확인 불가")
report_flags(os.environ, "실행 컨테이너 환경 · 운영 현물 기준")
db_ok = report_db()
if not db_ok:
    print("PostgreSQL 집계: 일부 또는 전체 확인 불가")
sys.exit(0 if env_ok and db_ok else 1)
' "$env_status" 2>/dev/null < "$input"; then
    printf '%s\n' '점검 상태: 추가 확인 필요'
    exit 1
fi
