from decimal import Decimal

from sqlalchemy import select, text

from app.core.config import ENABLE_SPOT_TESTNET_ORDERS, SPOT_TESTNET_MAX_ORDER_USDT
from app.models.spot_fill import SpotFill
from app.models.trading_account import TradingAccount
from app.models.user import User
from app.services.account_service import get_api_key_owned_by_user, get_or_create_bot_state
from app.services.binance_service import get_decrypted_api_keys
from app.services.spot_lifecycle import SpotLifecycle
from app.services.spot_service import SpotExchange

TEST_ENTRY = "spot-testnet-check"


def execute_bot_tick(db, account, *, recover_only=False, test_entry=None):
    if not ENABLE_SPOT_TESTNET_ORDERS or account.market_type != "SPOT":
        return {"status": "paused", "reason": "현물 테스트넷 주문 모드가 꺼져 있습니다."}
    account_id, key_id = account.id, account.api_key_id
    # Keep the lock across commits for the same testnet API key.
    with db.get_bind().connect() as lock:
        params = {"key": key_id}
        if not lock.execute(text("SELECT pg_try_advisory_lock(571126, :key)"), params).scalar():
            return {"status": "busy"}
        try:
            db.expire_all()
            account = db.get(TradingAccount, account_id)
            if not account or not account.is_active or account.market_type != "SPOT" or account.api_key_id != key_id:
                return {"status": "paused"}
            user = db.get(User, account.user_id)
            if not user or not user.is_active or not user.is_admin:
                return {"status": "paused"}
            key = get_api_key_owned_by_user(db, account.user_id, key_id)
            state = get_or_create_bot_state(db, account.id)
            lifecycle = SpotLifecycle(db, account, state, SpotExchange(*get_decrypted_api_keys(key)))
            try:
                if test_entry is None:
                    if not account.is_running or lifecycle.journal.get("signal_time") != TEST_ENTRY:
                        return {"status": "paused"}
                    result = lifecycle.advance()
                    if result["status"] == "flat":
                        account.is_running = False
                        db.commit()
                    return result
                if recover_only or account.is_running or lifecycle.journal or db.scalar(
                    select(SpotFill.trade_id).where(SpotFill.trading_account_id == account_id).limit(1)
                ) is not None:
                    return {"status": "blocked", "reason": "주문 검증은 새 검증 계좌에서 한 번만 가능합니다."}
                if lifecycle.exchange.open_order_lists():
                    return {"status": "blocked", "reason": "테스트넷 계정에 열린 보호주문이 있습니다."}
                price = lifecycle.exchange.price()
                stop = price * Decimal("0.99")
                target = price * Decimal("1.01")
                if test_entry == "reject_oco":
                    # Exercise the verified-inventory fallback without sending an invalid OCO.
                    stop = target
                account.is_running = True
                return lifecycle.entry(TEST_ENTRY, SPOT_TESTNET_MAX_ORDER_USDT, stop, target, quote=True)
            except Exception:
                db.rollback()
                return lifecycle.halt("현물 체결 또는 주문 상태 확인 실패. 자동 실행 중지.")
        finally:
            lock.execute(text("SELECT pg_advisory_unlock(571126, :key)"), params)
