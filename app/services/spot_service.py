"""BTCUSDT Spot exchange operations and the bot ownership boundary."""

from decimal import Decimal, InvalidOperation, ROUND_DOWN
from functools import lru_cache
from time import monotonic

from fastapi import HTTPException

from app.core.config import BINANCE_SPOT_BASE_URL
from app.services.binance_service import (
    OrderExecutionUnknown,
    binance_public_get,
    binance_signed_request,
)

SYMBOL = "BTCUSDT"


@lru_cache(maxsize=4)
def _filters(base_url, cache_window):
    data = binance_public_get(base_url, "/api/v3/exchangeInfo", {"symbol": SYMBOL})
    return {item["filterType"]: item for item in data["symbols"][0]["filters"]}


def _round(value, increment):
    value, increment = Decimal(value), Decimal(increment)
    if increment:
        value = (value / increment).to_integral_value(rounding=ROUND_DOWN) * increment
    return value


def normalize_order(filters, quantity, prices, market=False):
    """Round down before validating limits, including both OCO leg prices."""
    quantity = Decimal(quantity)
    if not quantity.is_finite() or quantity <= 0:
        raise ValueError("주문 수량은 양수여야 합니다.")
    lots = [filters["LOT_SIZE"]]
    if market and "MARKET_LOT_SIZE" in filters:
        lots.append(filters["MARKET_LOT_SIZE"])
    for lot in lots:
        quantity = _round(quantity, lot["stepSize"])
    for lot in lots:
        step = Decimal(lot["stepSize"])
        maximum = Decimal(lot["maxQty"])
        if quantity <= 0 or quantity < Decimal(lot["minQty"]) or (maximum and quantity > maximum):
            raise ValueError("주문 수량이 거래소 한도를 벗어났습니다.")
        if step and quantity % step:
            raise ValueError("주문 수량 단위가 거래소 규칙과 일치하지 않습니다.")

    price_filter = filters["PRICE_FILTER"]
    normalized = []
    for price in prices:
        price = Decimal(price)
        if not price.is_finite() or price <= 0:
            raise ValueError("주문 가격은 양수여야 합니다.")
        if not market:
            price = _round(price, price_filter["tickSize"])
            maximum = Decimal(price_filter["maxPrice"])
            if price <= 0 or price < Decimal(price_filter["minPrice"]) or (maximum and price > maximum):
                raise ValueError("주문 가격이 거래소 한도를 벗어났습니다.")
        for name, min_flag, max_flag in (
            ("MIN_NOTIONAL", "applyToMarket", None),
            ("NOTIONAL", "applyMinToMarket", "applyMaxToMarket"),
        ):
            rule = filters.get(name)
            if rule is None:
                continue
            notional = quantity * price
            if (not market or rule.get(min_flag)) and notional < Decimal(rule["minNotional"]):
                raise ValueError("주문 금액이 거래소 최소 금액에 미달합니다.")
            if max_flag and (not market or rule.get(max_flag)) and notional > Decimal(rule["maxNotional"]):
                raise ValueError("주문 금액이 거래소 최대 금액을 초과합니다.")
        normalized.append(price)
    return quantity, normalized


def confirmed_bot_quantity(fills):
    """Only verified exchange fills owned by this bot establish its BTC inventory."""
    if not fills:
        return None
    remaining = Decimal(0)
    seen = set()
    try:
        for fill in fills:
            if fill["trade_id"] in seen:
                return None
            seen.add(fill["trade_id"])
            quantity = Decimal(fill["quantity"])
            commission = Decimal(fill["commission"])
            if not quantity.is_finite() or not commission.is_finite():
                return None
            if quantity <= 0 or commission < 0:
                return None
            if fill["side"] == "BUY":
                remaining += quantity
            elif fill["side"] == "SELL":
                remaining -= quantity
            else:
                return None
            if fill["commission_asset"] == "BTC":
                remaining -= commission
            if remaining < 0:
                return None
    except (InvalidOperation, KeyError, TypeError, ValueError):
        return None
    return remaining


class SpotExchange:
    def __init__(self, api_key, secret_key):
        self.api_key = api_key
        self.secret_key = secret_key

    def _signed(self, method, path, params=None):
        return binance_signed_request(
            method, BINANCE_SPOT_BASE_URL, path, self.api_key, self.secret_key, params,
        )

    def _submit(self, path, params, lookup_path, client_id, list_order=False):
        try:
            return self._signed("POST", path, params)
        except OrderExecutionUnknown:
            lookup = {"origClientOrderId": client_id}
            if not list_order:
                lookup["symbol"] = SYMBOL
            try:
                return self._signed("GET", lookup_path, lookup)
            except HTTPException:
                raise OrderExecutionUnknown("조회 후에도 현물 주문 상태를 확인할 수 없습니다.") from None

    def _normalize(self, quantity, prices, market=False):
        filters = _filters(BINANCE_SPOT_BASE_URL, int(monotonic() // 300))
        return normalize_order(filters, quantity, prices, market)

    def _reference_prices(self):
        data = binance_public_get(BINANCE_SPOT_BASE_URL, "/api/v3/ticker/price", {"symbol": SYMBOL})
        average = binance_public_get(BINANCE_SPOT_BASE_URL, "/api/v3/avgPrice", {"symbol": SYMBOL})
        return [data["price"], average["price"]]

    def order(self, side, quantity, client_id, limit_price=None):
        if side not in {"BUY", "SELL"}:
            raise ValueError("현물 주문 방향이 올바르지 않습니다.")
        market = limit_price is None
        prices = self._reference_prices() if market else [limit_price]
        quantity, prices = self._normalize(quantity, prices, market)
        params = {
            "symbol": SYMBOL,
            "side": side,
            "type": "MARKET" if market else "LIMIT",
            "quantity": format(quantity, "f"),
            "newClientOrderId": client_id,
            "newOrderRespType": "FULL",
        }
        if not market:
            params.update(price=format(prices[0], "f"), timeInForce="GTC")
        return self._submit("/api/v3/order", params, "/api/v3/order", client_id)

    def market_buy_quote(self, quote, client_id):
        return self._submit(
            "/api/v3/order",
            {
                "symbol": SYMBOL, "side": "BUY", "type": "MARKET",
                "quoteOrderQty": format(quote, "f"), "newClientOrderId": client_id,
                "newOrderRespType": "FULL",
            },
            "/api/v3/order", client_id,
        )

    def protect(self, quantity, stop_price, take_profit, client_id):
        quantity, (stop_price, take_profit) = self._normalize(quantity, [stop_price, take_profit])
        if stop_price >= take_profit:
            raise ValueError("손절가는 익절가보다 낮아야 합니다.")
        return self._submit(
            "/api/v3/orderList/oco",
            {
                "symbol": SYMBOL, "side": "SELL", "quantity": format(quantity, "f"),
                "listClientOrderId": client_id, "aboveType": "LIMIT_MAKER",
                "abovePrice": format(take_profit, "f"), "belowType": "STOP_LOSS",
                "belowStopPrice": format(stop_price, "f"),
            },
            "/api/v3/orderList", client_id, list_order=True,
        )

    def lookup(self, client_id, *, order_list=False):
        params = {"origClientOrderId": client_id}
        if not order_list:
            params["symbol"] = SYMBOL
        try:
            return self._signed("GET", "/api/v3/orderList" if order_list else "/api/v3/order", params)
        except HTTPException as exc:
            if isinstance(exc.detail, dict) and exc.detail.get("binance_code") in {-2013, -2026}:
                return None
            raise

    def trades(self, order_id):
        return self._signed("GET", "/api/v3/myTrades", {"symbol": SYMBOL, "orderId": order_id, "limit": 1000})

    def price(self):
        response = binance_public_get(BINANCE_SPOT_BASE_URL, "/api/v3/ticker/price", {"symbol": SYMBOL})
        return Decimal(response["price"])

    def account(self):
        return self._signed("GET", "/api/v3/account")

    def open_order_lists(self):
        return self._signed("GET", "/api/v3/openOrderList")


def recovery_quantity(fills, free_btc, locked_btc):
    """Cap recovery to bot-owned BTC; unknown ownership requires a manual halt.

    Unrelated BTC is never included. Locked BTC may already be protected, so
    a caller must verify the bot's order list before using the sellable amount.
    """
    owned = confirmed_bot_quantity(fills)
    if owned is None:
        return {"halt": True, "quantity": Decimal(0), "alert": "봇 체결 기록 확인 필요"}
    try:
        free_btc, locked_btc = Decimal(free_btc), Decimal(locked_btc)
        if any(not value.is_finite() or value < 0 for value in (free_btc, locked_btc)):
            raise ValueError("invalid balance")
    except (InvalidOperation, TypeError, ValueError):
        return {"halt": True, "quantity": Decimal(0), "alert": "거래소 잔고 확인 필요"}
    balance = free_btc + locked_btc
    return {
        "halt": False,
        "quantity": min(owned, free_btc),
        "alert": "거래소 잔고와 봇 보유량 불일치" if balance != owned else None,
    }
