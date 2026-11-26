"""Fixed BTCUSDT spot constraints for historical replay."""

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN


@dataclass(frozen=True)
class SpotOrderRules:
    quantity_step: Decimal = Decimal("0.00001")
    minimum_notional: float = 5.0

    def buy_quantity(self, requested, fill_price):
        steps = (Decimal(str(requested)) / self.quantity_step).to_integral_value(rounding=ROUND_DOWN)
        quantity = float(steps * self.quantity_step)
        return quantity if self.can_sell(quantity, fill_price) else 0.0

    def can_sell(self, quantity, fill_price):
        return quantity > 0 and quantity * fill_price >= self.minimum_notional
