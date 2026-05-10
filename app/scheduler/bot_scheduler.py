from apscheduler.schedulers.background import BackgroundScheduler

from app.core.config import BOT_INTERVAL_SECONDS
from app.db.session import SessionLocal
from app.models.trading_account import TradingAccount
from app.services.bot_service import execute_bot_tick
from app.services.error_log_service import create_error_log

scheduler = BackgroundScheduler()


def get_job_id(trading_account_id: int) -> str:
    return f'trading_account_{trading_account_id}'


def bot_tick_job(trading_account_id: int):
    db = SessionLocal()
    try:
        account = db.query(TradingAccount).filter(TradingAccount.id == trading_account_id).first()
        if not account or not account.is_running:
            return
        execute_bot_tick(db=db, account=account)
    except Exception as exc:
        try:
            create_error_log(
                db=db,
                trading_account_id=trading_account_id,
                source='bot_tick_job',
                message=str(exc),
            )
        except Exception:
            db.rollback()
    finally:
        db.close()


def start_account_job(trading_account_id: int):
    job_id = get_job_id(trading_account_id)
    existing = scheduler.get_job(job_id)
    if existing:
        existing.remove()

    scheduler.add_job(
        bot_tick_job,
        trigger='interval',
        seconds=BOT_INTERVAL_SECONDS,
        args=[trading_account_id],
        id=job_id,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30,
    )


def stop_account_job(trading_account_id: int):
    job_id = get_job_id(trading_account_id)
    existing = scheduler.get_job(job_id)
    if existing:
        existing.remove()


def recover_running_account_jobs():
    db = SessionLocal()
    recovered = 0
    try:
        accounts = db.query(TradingAccount).filter(TradingAccount.is_running == True).all()
        for account in accounts:
            start_account_job(account.id)
            recovered += 1
        return recovered
    finally:
        db.close()


def start_scheduler():
    if not scheduler.running:
        scheduler.start()
    return recover_running_account_jobs()


def shutdown_scheduler():
    if scheduler.running:
        scheduler.shutdown()
