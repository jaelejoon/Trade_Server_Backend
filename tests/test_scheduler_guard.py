import pytest
from fastapi import HTTPException

from app.routers import trading
from app.scheduler import bot_scheduler


def test_disabled_scheduler_skips_startup_and_account_jobs(monkeypatch):
    monkeypatch.setattr(bot_scheduler, "ENABLE_TRADING_SCHEDULER", False)

    def unexpected(*args, **kwargs):
        pytest.fail("Disabled scheduler accessed the scheduler or database")

    monkeypatch.setattr(bot_scheduler.scheduler, "start", unexpected)
    monkeypatch.setattr(bot_scheduler.scheduler, "get_job", unexpected)
    monkeypatch.setattr(bot_scheduler, "SessionLocal", unexpected)
    assert bot_scheduler.start_scheduler() == 0
    bot_scheduler.start_account_job(1)
    bot_scheduler.bot_tick_job(1)


def test_disabled_scheduler_rejects_start_before_warmup(monkeypatch):
    monkeypatch.setattr(trading, "ENABLE_TRADING_SCHEDULER", False)
    with pytest.raises(HTTPException) as exc:
        trading.start_trading_account(1, db=None, current_user=None)
    assert exc.value.status_code == 409
