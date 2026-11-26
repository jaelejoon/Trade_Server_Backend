from decimal import Decimal

from sqlalchemy import text

from app.core.config import BINANCE_TESTNET, DRY_RUN, ENABLE_LIVE_TRADING
from app.models.trading_account import TradingAccount
from app.models.user import User
from app.services.account_service import get_api_key_owned_by_user, get_or_create_bot_state
from app.services.app_setting_service import get_live_trading_enabled
from app.services.binance_service import get_decrypted_api_keys
from app.services.spot_lifecycle import SpotLifecycle
from app.services.spot_service import SpotExchange
from app.services.strategy_signal_service import get_v57_signal_from_db_candles


def is_account_live_trading_enabled(db, account):
    if DRY_RUN or not BINANCE_TESTNET or not ENABLE_LIVE_TRADING or account.dry_run:
        return False
    user = db.get(User, account.user_id)
    return bool(user and user.is_active and not user.dry_run and user.enable_live_trading
                and get_live_trading_enabled(db))


def execute_bot_tick(db, account, *, recover_only=False):
    if account.market_type != "SPOT" or not is_account_live_trading_enabled(db, account):
        return {"status": "paused", "reason": "현물 테스트넷 주문 모드가 꺼져 있습니다."}
    account_id, key_id = account.id, account.api_key_id
    # Hold a session-level lock across commits to serialize journaled requests for the same key.
    with db.get_bind().connect() as lock:
        params = {"key": key_id}
        if not lock.execute(text("SELECT pg_try_advisory_lock(571126, :key)"), params).scalar():
            return {"status": "busy"}
        try:
            db.expire_all()
            account = db.get(TradingAccount, account_id)
            if not account or not account.is_active or not account.is_running or not is_account_live_trading_enabled(db, account):
                return {"status": "paused"}
            key = get_api_key_owned_by_user(db, account.user_id, key_id)
            state = get_or_create_bot_state(db, account.id)
            lifecycle = SpotLifecycle(db, account, state, SpotExchange(*get_decrypted_api_keys(key)))
            try:
                result = lifecycle.advance()
                if recover_only or result["status"] != "flat":
                    return result
                signal_result = get_v57_signal_from_db_candles(
                    db, account.user_id, account.symbol, trading_account_id=account.id,
                )
                signal = signal_result.get("signal")
                if signal is None:
                    return {"status": "no_signal", "reason": "닫힌 현물 봉 확인 필요"}
                if not signal["has_signal"] or signal["signal_time"] == lifecycle.journal.get("signal_time"):
                    return {"status": "no_signal"}
                price = lifecycle.exchange.price()
                gap = Decimal(str(signal["atr"])) * Decimal(str(signal["stop_atr_mult"]))
                balance = next((Decimal(row["free"]) for row in lifecycle.exchange.account()["balances"]
                                if row["asset"] == "USDT"), Decimal(0))
                risk = min(Decimal(str(signal["risk_per_trade"])), Decimal(str(account.max_risk_ratio)))
                cap = min(Decimal(str(signal["max_position_ratio"])), Decimal("0.99"))
                if not 0 < gap < price or balance <= 0 or risk <= 0 or cap <= 0:
                    return {"status": "no_order"}
                quantity = min(balance * risk / gap, balance * cap / price)
                target = price + gap * Decimal(str(signal["take_profit_r"]))
                return lifecycle.entry(signal["signal_time"], quantity, price - gap, target)
            except Exception:
                db.rollback()
                return lifecycle.halt("현물 체결 또는 주문 상태 확인 실패. 자동 실행 중지.")
        finally:
            lock.execute(text("SELECT pg_advisory_unlock(571126, :key)"), params)
