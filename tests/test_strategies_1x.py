import pandas as pd
import pytest

from app.services.backtest_engine import (
    STRATEGIES, BacktestInputError, LossLimits, Signal, replay,
)


SAFE_STRATEGIES = [strategy for strategy in STRATEGIES.values() if strategy.loss_limits]


@pytest.mark.parametrize("strategy", SAFE_STRATEGIES)
@pytest.mark.parametrize("side", [1, -1])
def test_breakout_uses_closed_hours_and_next_open(strategy, side):
    minutes = 50 * 60 + 1
    index = pd.date_range("2025-01-01", periods=minutes, freq="min")
    bars = pd.DataFrame(100.0, index=index, columns=["open", "high", "low", "close", "volume"])
    bars.iloc[-2, :4] = 100 + side * 10
    signals = strategy.signals(bars)
    assert list(signals) == [index[-1]]
    assert signals[index[-1]].side == side
    assert not strategy.signals(bars.iloc[:-2])
    bars.iloc[-1, :4] = 1000
    assert strategy.signals(bars) == signals
    assert not strategy.signals(bars.drop(index[-3]))
    result = replay(bars, signals, 1000, 1, 5, 2, {}, loss_limits=strategy.loss_limits)
    assert result["trades"][0]["entry_time"] == index[-1].isoformat() + "Z"
    assert result["metrics"]["max_entry_exposure_pct"] <= signals[index[-1]].max_position_fraction * 100


@pytest.mark.parametrize("side", [1, -1])
def test_at_stop_fees_and_slippage_fit_risk_budget(side):
    index = pd.date_range("2025-01-01", periods=1, freq="min")
    bars = pd.DataFrame([(100, 110, 90, 100, 1)], index=index,
                        columns=["open", "high", "low", "close", "volume"])
    signal = Signal(side, 3, 2, 0.004, 0.30, 60)
    result = replay(bars, {index[0]: signal}, 1000, 1, 5, 2, {}, loss_limits=LossLimits(.015, .10))
    trade = result["trades"][0]
    assert trade["exit_reason"] == "stop_loss"
    assert trade["net_pnl_usdt"] == pytest.approx(-4)
    with pytest.raises(BacktestInputError, match="레버리지"):
        replay(bars, {index[0]: signal}, 1000, 2, 5, 2, {}, loss_limits=LossLimits(.015, .10))


@pytest.mark.parametrize("peak_limit,expected", [(0.15, [0, 2]), (0.04, [0])])
def test_daily_halt_resets_at_utc_midnight_but_peak_halt_persists(peak_limit, expected):
    index = pd.date_range("2025-01-01 23:58", periods=4, freq="min")
    bars = pd.DataFrame([(100, 100, 90, 91, 1)] * len(index), index=index,
                        columns=["open", "high", "low", "close", "volume"])
    signals = dict.fromkeys(index, Signal(1, 10, 2, 0.05, 0.5, 60))
    result = replay(bars, signals, 1000, 1, 0, 0, {}, loss_limits=LossLimits(0.02, peak_limit))
    assert [trade["entry_time"] for trade in result["trades"]] == [
        index[i].isoformat() + "Z" for i in expected
    ]
    assert bool(result["metrics"]["peak_halt_time"]) == (peak_limit == 0.04)
