import pandas as pd
import pytest

from app.services.backtest_engine import STRATEGIES
from app.services.strategy_1x_regimes import MonthlyRegime, replay_regime


def test_month_end_signal_does_not_read_next_minutes_close():
    index = pd.to_datetime([
        "2024-01-31 23:59", "2024-02-29 23:59", "2024-03-01 00:00",
    ])
    candles = pd.DataFrame({"close": [100, 110, 1]}, index=index)
    regime = MonthlyRegime(2, 0.9)
    decisions = regime.decisions(candles)
    assert decisions == {index[-1]: True}
    candles.loc[index[-1], "close"] = 1_000
    assert regime.decisions(candles) == decisions
    assert regime.decisions(candles.drop(index[1])) == {}


def test_regime_fills_at_next_open_and_keeps_existing_quantity():
    index = pd.date_range("2024-01-01", periods=5, freq="min")
    candles = pd.DataFrame({"open": [100, 110, 130, 140, 90],
                            "close": [110, 125, 135, 120, 100]}, index=index)
    result = replay_regime(
        candles, {index[1]: True, index[2]: True, index[3]: False},
        0.9, 1000, 5, 2, {index[2]: (130, .01)},
    )
    trade, = result["trades"]
    assert trade["entry_time"] == index[1].isoformat() + "Z"
    assert trade["entry_price"] == pytest.approx(110 * 1.0002)
    assert trade["exit_price"] == pytest.approx(140 * .9998)
    assert trade["quantity"] * trade["entry_price"] + trade["entry_price"] * trade["quantity"] * .0005 <= 900
    assert trade["funding_cost_usdt"] == pytest.approx(trade["quantity"] * 1.3)
    assert result["metrics"]["final_equity_usdt"] == pytest.approx(1000 + trade["net_pnl_usdt"])
    assert result["equity_curve"][-1][1] == pytest.approx(result["metrics"]["final_equity_usdt"])


def test_funding_that_exhausts_cash_buffer_exits_at_open():
    index = pd.date_range("2024-01-01", periods=3, freq="min")
    candles = pd.DataFrame({"open": [100, 100, 150], "close": [100, 150, 150]}, index=index)
    result = replay_regime(candles, {index[0]: True}, .9, 1000, 0, 0, {index[1]: (100, .15)})
    trade, = result["trades"]
    assert trade["exit_reason"] == "exposure_limit"
    assert trade["exit_time"] == index[1].isoformat() + "Z"
    assert trade["exit_price"] == 100


def test_v2_strategies_are_one_x_only():
    strategies = [strategy for strategy in STRATEGIES.values() if strategy.version == "2"]
    assert len(strategies) == 3
    assert all(strategy.one_x_only and 0 < strategy.regime.exposure < 1 for strategy in strategies)
