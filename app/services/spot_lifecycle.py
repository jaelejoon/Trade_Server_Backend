"""Durable client IDs and reconciled fills for the BTCUSDT bot."""

from decimal import Decimal
from uuid import uuid4

from fastapi import HTTPException

from app.services.binance_service import OrderExecutionUnknown
from app.services.error_log_service import create_error_log
from app.services.spot_recovery_service import inspect_recovery, record_order_fills

TERMINAL = {"FILLED", "CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH"}


class SpotLifecycle:
    def __init__(self, db, account, state, exchange):
        self.db = db
        self.account = account
        self.state = state
        self.exchange = exchange
        self.journal = dict(state.runtime_state.get("spot", {}))

    def save(self, **changes):
        self.journal.update(changes)
        self.state.runtime_state = {**self.state.runtime_state, "spot": dict(self.journal)}
        self.db.commit()

    def alert(self, message):
        create_error_log(
            self.db, "spot_lifecycle", message, user_id=self.account.user_id,
            trading_account_id=self.account.id, level="WARNING",
        )

    def halt(self, reason):
        self.account.is_running = False
        self.save(halted=True)
        self.alert(reason)
        return {"status": "stopped", "reason": reason}

    def ingest(self, order, side):
        if order["side"] != side:
            raise ValueError("저널 주문 방향 불일치")
        executed = Decimal(order["executedQty"])
        if executed:
            order_id = order["orderId"]
            trades = self.exchange.trades(order_id)
            if any(trade["isBuyer"] != (side == "BUY") for trade in trades):
                raise ValueError("체결 방향 불일치")
            if sum(Decimal(trade["qty"]) for trade in trades) != executed:
                raise ValueError("체결 수량 대조 실패")
            record_order_fills(self.db, self.account.id, order_id, trades)
            self.db.commit()
        return order["status"] in TERMINAL

    def entry(self, signal_time, quantity, stop, target):
        order_id = f"spot-buy-{uuid4().hex[:24]}"
        self.save(buy=order_id, signal_time=signal_time, stop=str(stop), target=str(target))
        order = self.exchange.order("BUY", quantity, order_id)
        return self.advance(buy_response=order)

    def protect(self, quantity):
        client_id = f"spot-oco-{uuid4().hex[:24]}"
        self.save(oco=client_id)
        try:
            self.exchange.protect(quantity, self.journal["stop"], self.journal["target"], client_id)
        except OrderExecutionUnknown:
            return self.halt("보호주문 상태 미확인. 추가 주문 중지.")
        except (HTTPException, ValueError):
            # Confirm again after the rejected OCO and only sell verified free bot BTC.
            recovery, open_lists = inspect_recovery(self.db, self.account.id, self.exchange)
            if recovery["halt"] or not recovery["quantity"] or open_lists:
                return self.halt("보호주문 실패 후 매도 가능 수량 확인 필요.")
            sale_id = f"spot-sell-{uuid4().hex[:23]}"
            self.save(sell=sale_id)
            sale = self.exchange.order("SELL", recovery["quantity"], sale_id)
            if self.ingest(sale, "SELL"):
                self.save(sell=None, oco=None)
            return self.halt("보호주문 등록 실패. 봇 보유분 비상 매도 요청 완료.")
        return {"status": "protected", "client_order_list_id": client_id}

    def advance(self, buy_response=None):
        if self.journal.get("halted"):
            return {"status": "stopped", "reason": "봇 주문 상태 수동 확인 필요"}
        if self.journal.get("sell"):
            return self.halt("이전 비상 매도 상태 수동 확인 필요.")
        if self.journal.get("buy"):
            order = buy_response or self.exchange.lookup(self.journal["buy"])
            if order is None:
                return self.halt("매수 주문의 상태를 확인할 수 없습니다.")
            if not self.ingest(order, "BUY"):
                return self.halt("매수 주문이 종료되지 않았습니다. 보호 범위 확인 필요.")
            self.save(buy=None)

        oco_id = self.journal.get("oco")
        finished = None
        if oco_id:
            finished = self.exchange.lookup(oco_id, order_list=True)
            if finished is None:
                return self.halt("기존 보호주문 상태를 확인할 수 없습니다.")
            for leg in finished["orders"]:
                order = self.exchange.lookup(leg["clientOrderId"])
                if order is None:
                    return self.halt("보호주문 하위 주문 상태를 확인할 수 없습니다.")
                self.ingest(order, "SELL")

        recovery, open_lists = inspect_recovery(self.db, self.account.id, self.exchange)
        if recovery["halt"]:
            if recovery["balance"] == 0 and not open_lists and not self.journal.get("stop"):
                return {"status": "flat"}
            return self.halt("봇 체결 기록을 확인할 수 없습니다.")
        if recovery["alert"]:
            self.alert(recovery["alert"])
        if oco_id and any(item["listClientOrderId"] == oco_id for item in open_lists):
            return {"status": "protected", "client_order_list_id": oco_id}
        if oco_id and finished["listOrderStatus"] != "ALL_DONE":
            return self.halt("보호주문의 종료 상태를 확인할 수 없습니다.")
        if oco_id:
            self.save(oco=None)
        if not recovery["owned"]:
            return {"status": "flat"}
        if not recovery["quantity"] or open_lists or not self.journal.get("stop") or not self.journal.get("target"):
            return self.halt("봇 보유분의 보호주문 복구 조건을 확인할 수 없습니다.")
        return self.protect(recovery["quantity"])
