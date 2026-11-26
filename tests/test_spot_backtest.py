import pandas as pd
import pytest

from app.services.backtest_engine import BacktestInputError, LossLimits, Signal, replay
from app.services.spot_backtest import SpotOrderRules
from app.services.strategy_1x_regimes import replay_regime


def test_spot_exchange_filters_round_down_and_enforce_minimum():
    rules = SpotOrderRules()
    assert rules.buy_quantity(0.0000199, 500_000) == 0.00001
    assert rules.buy_quantity(0.0000199, 499_999) == 0.0
    assert rules.can_sell(0.00001, 500_000)
    assert not rules.can_sell(0.00001, 499_999)


def test_spot_hold_charges_both_fees_without_overspending():
    index = pd.date_range("2024-01-01", periods=2, freq="min")
    candles = pd.DataFrame({"open": [101, 190], "close": [110, 200]}, index=index)
    result = replay_regime(candles, {index[0]: True}, 1, 1000, 10, 2, {}, spot=SpotOrderRules())
    trade, = result["trades"]
    assert trade["quantity"] == pytest.approx(9.88912)
    assert trade["quantity"] * trade["entry_price"] * 1.001 <= 1000
    assert trade["fees_usdt"] == pytest.approx(trade["quantity"] * (101 * 1.0002 + 200 * .9998) * .001)
    assert result["metrics"]["final_equity_usdt"] == pytest.approx(1000 + trade["net_pnl_usdt"])


def test_spot_below_minimum_exit_is_marked_without_fictitious_fill():
    index = pd.date_range("2024-01-01", periods=2, freq="min")
    candles = pd.DataFrame({"open": [100, 4], "close": [100, 4]}, index=index)
    result = replay_regime(candles, {index[0]: True}, 1, 100, 0, 0, {}, spot=SpotOrderRules())
    assert result["trades"] == []
    assert result["metrics"]["rejected_sells"] == 1
    assert result["metrics"]["final_equity_usdt"] == 4


def test_legacy_spot_replay_uses_stop_without_liquidation_and_rejects_short():
    index = pd.date_range("2024-01-01", periods=2, freq="min")
    candles = pd.DataFrame(
        [(100, 100, 100, 100), (1, 1, 1, 1)], index=index,
        columns=["open", "high", "low", "close"],
    )
    kwargs = {"loss_limits": LossLimits(.015, .10), "spot": SpotOrderRules()}
    long = Signal(1, 3, 2, .004, .3, 60)
    result = replay(candles, {index[0]: long}, 10_000, 1, 0, 0, {}, **kwargs)
    trade, = result["trades"]
    assert trade["exit_reason"] == "stop_loss"
    short = Signal(-1, 3, 2, .004, .3, 60)
    with pytest.raises(BacktestInputError, match="현물"):
        replay(candles, {index[0]: short}, 10_000, 1, 0, 0, {}, **kwargs)
