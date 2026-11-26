"""Causal BTCUSDT replay, independent of accounts and order execution."""

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from hashlib import sha256
import numpy as np
import pandas as pd

from app.strategy import strategy_v57 as v57


MINUTE = pd.Timedelta(minutes=1)
ENGINE_VERSION = "1"
CANDLE_COLUMNS = ["open", "high", "low", "close", "volume"]


class BacktestInputError(ValueError):
    pass


@dataclass(frozen=True)
class Signal:
    side: int
    stop_distance: float
    reward_multiple: float
    risk_fraction: float
    max_position_fraction: float
    max_hold_minutes: int


def v57_signals(candles):
    with v57.V57ConfigOverride(v57.DEFAULT_V57_CONFIG):
        hours, quarters = v57.prepare_closed_candles(candles)
        events = v57.build_signal_events(hours, quarters)
        signals = {}
        for event in events.itertuples(index=False):
            config = v57.get_strategy_config(event.strategy, event.market_regime, event.event_risk)
            if event.atr > 0:
                signals[event.time] = Signal(
                    1 if event.side == "LONG" else -1,
                    event.atr * config["stop_atr_mult"],
                    config["take_profit_r"],
                    config["risk_per_trade"],
                    config["max_position_ratio"],
                    config["max_hold_bars"] * 15,
                )
        return signals


@dataclass(frozen=True)
class LossLimits:
    daily: float
    peak: float


@dataclass(frozen=True)
class Strategy:
    version: str
    warmup: pd.Timedelta
    signals: Callable[[pd.DataFrame], dict[pd.Timestamp, Signal]]
    loss_limits: LossLimits | None = None


def breakout_signals(candles, lookback, stop_fraction, reward, risk, exposure, hold_hours):
    hourly = candles.resample("1h", label="right", closed="left").agg(
        high=("high", "max"), low=("low", "min"),
        close=("close", "last"), count=("close", "count"),
    )
    complete = hourly["count"].eq(60)
    prior_high = hourly.high.where(complete).shift().rolling(lookback).max()
    prior_low = hourly.low.where(complete).shift().rolling(lookback).min()
    longs = complete & hourly.close.gt(prior_high)
    shorts = complete & hourly.close.lt(prior_low)
    return {
        time: Signal(
            1 if longs.at[time] else -1,
            close * stop_fraction, reward, risk,
            exposure if longs.at[time] else exposure / 2, hold_hours * 60,
        )
        for time, close in hourly.loc[longs | shorts, "close"].items()
    }


STRATEGIES = {
    "V57_LONG_SAFE_PUSH": Strategy("1", pd.Timedelta(hours=4 * 200), v57_signals),
    "SAFE_1X_AGGRESSIVE": Strategy(
        "1", pd.Timedelta(hours=12 + 1),
        partial(breakout_signals, lookback=12, stop_fraction=0.025, reward=2.0,
                risk=0.0075, exposure=0.50, hold_hours=48),
        LossLimits(daily=0.025, peak=0.15),
    ),
    "SAFE_1X_BALANCED": Strategy(
        "1", pd.Timedelta(hours=24 + 1),
        partial(breakout_signals, lookback=24, stop_fraction=0.03, reward=2.5,
                risk=0.004, exposure=0.30, hold_hours=72),
        LossLimits(daily=0.015, peak=0.10),
    ),
    "SAFE_1X_DEFENSIVE": Strategy(
        "1", pd.Timedelta(hours=48 + 1),
        partial(breakout_signals, lookback=48, stop_fraction=0.04, reward=3.0,
                risk=0.002, exposure=0.15, hold_hours=96),
        LossLimits(daily=0.008, peak=0.06),
    ),
}


def validate_candles(candles, start, end):
    expected = pd.date_range(start, end, freq="min", inclusive="left")
    if not candles.index.equals(expected):
        raise BacktestInputError("워밍업 또는 실행 구간의 분봉이 누락되었거나 중복되었습니다.")
    prices = candles[["open", "high", "low", "close"]]
    invalid = (
        not np.isfinite(candles.to_numpy()).all()
        or (prices <= 0).any().any()
        or (candles.volume < 0).any()
        or (candles.high < prices.max(axis=1)).any()
        or (candles.low > prices.min(axis=1)).any()
    )
    if invalid:
        raise BacktestInputError("분봉에 유효하지 않은 OHLCV가 있습니다.")


@dataclass
class Position:
    time: pd.Timestamp
    price: float
    side: int
    quantity: float
    entry_fee: float
    stop: float
    target: float
    expires: pd.Timestamp
    funding_cost: float = 0.0

    def pnl(self, price):
        return self.side * self.quantity * (price - self.price)

    def close_price(self, bar, liquidation):
        for price, reason in ((liquidation, "liquidation"), (self.stop, "stop_loss")):
            if (bar.open - price) * self.side <= 0:
                return bar.open, reason
        if (bar.open - self.target) * self.side >= 0:
            return bar.open, "take_profit"
        if bar.Index >= self.expires:
            return bar.open, "max_hold"
        adverse, favorable = (bar.low, bar.high) if self.side == 1 else (bar.high, bar.low)
        if (adverse - self.stop) * self.side <= 0:
            # The protective stop was crossed first if it lies before liquidation.
            if (self.stop - liquidation) * self.side > 0:
                return self.stop, "stop_loss"
        if (adverse - liquidation) * self.side <= 0:
            return liquidation, "liquidation"
        if (adverse - self.stop) * self.side <= 0:
            return self.stop, "stop_loss"
        if (favorable - self.target) * self.side >= 0:
            return self.target, "take_profit"
        return None, None


def replay(
    candles, signals, initial_equity, leverage, fee_bps, slippage_bps, funding,
    *, loss_limits=None, include_curve=True,
):
    """Signals indexed by availability time fill at that minute's open."""
    if loss_limits is not None and leverage != 1:
        raise BacktestInputError("1배 전용 전략은 레버리지 1만 허용합니다.")
    fee_rate, slip = fee_bps / 10_000, slippage_bps / 10_000
    balance = peak = initial_equity
    mdd = 0.0
    position = None
    trades, curve = [], []
    if include_curve:
        curve.append([candles.index[0].isoformat() + "Z", initial_equity])
    equity = day_start_equity = initial_equity
    day = None
    daily_halt = peak_halt = False
    daily_halts = 0
    peak_halt_time = None
    max_entry_exposure_pct = 0.0

    for bar in candles.itertuples():
        time = bar.Index
        signal = signals.get(time)
        if loss_limits is not None:
            utc_day = time.date()
            if day != utc_day:
                day, day_start_equity, daily_halt = utc_day, equity, False
        if position and time in funding:
            mark_price, rate = funding[time]
            cost = position.side * position.quantity * mark_price * rate
            position.funding_cost += cost
            balance -= cost

        if position is None and signal and balance > 0 and not (daily_halt or peak_halt):
            entry = bar.open * (1 + signal.side * slip)
            stop = entry - signal.side * signal.stop_distance
            risk_per_unit = signal.stop_distance
            if loss_limits is not None:
                stop_fill = stop * (1 - signal.side * slip)
                risk_per_unit = signal.side * (entry - stop_fill) + fee_rate * (entry + stop_fill)
            quantity = min(
                balance * signal.risk_fraction / risk_per_unit,
                balance * signal.max_position_fraction / entry,
                balance / (entry * (1 / leverage + fee_rate)),
            )
            if quantity > 0 and (loss_limits is None or stop > 0):
                max_entry_exposure_pct = max(max_entry_exposure_pct, quantity * entry / balance * 100)
                fee = quantity * entry * fee_rate
                balance -= fee
                position = Position(
                    time, entry, signal.side, quantity, fee,
                    stop,
                    entry + signal.side * signal.stop_distance * signal.reward_multiple,
                    time + signal.max_hold_minutes * MINUTE,
                )

        if position:
            # Fixed 0.5% maintenance margin approximation on position notional.
            margin = position.price / leverage - position.funding_cost / position.quantity
            liquidation = (position.price * position.side - margin) / (position.side - 0.005)
            price, reason = position.close_price(bar, liquidation)
            exit_time = time
            if price is None and time == candles.index[-1]:
                price, reason, exit_time = bar.close, "end_of_period", time + MINUTE
            if price is not None:
                exit_price = price * (1 - position.side * slip)
                exit_fee = position.quantity * exit_price * fee_rate
                gross = position.pnl(exit_price)
                balance += gross - exit_fee
                trades.append({
                    "entry_time": position.time.isoformat() + "Z",
                    "exit_time": exit_time.isoformat() + "Z",
                    "side": "LONG" if position.side == 1 else "SHORT",
                    "entry_price": position.price,
                    "exit_price": exit_price,
                    "quantity": position.quantity,
                    "fees_usdt": position.entry_fee + exit_fee,
                    "funding_cost_usdt": position.funding_cost,
                    "net_pnl_usdt": gross - position.entry_fee - exit_fee - position.funding_cost,
                    "exit_reason": reason,
                })
                position = None

        equity = balance + (position.pnl(bar.close) if position else 0.0)
        peak = max(peak, equity)
        mdd = max(mdd, 1 - equity / peak)
        if loss_limits is not None:
            if not daily_halt and equity <= day_start_equity * (1 - loss_limits.daily):
                daily_halt = True
                daily_halts += 1
            if not peak_halt and equity <= peak * (1 - loss_limits.peak):
                peak_halt = True
                peak_halt_time = (time + MINUTE).isoformat() + "Z"
        if include_curve:
            curve.append([(time + MINUTE).isoformat() + "Z", equity])

    gains = losses = 0.0
    wins = consecutive_losses = longest_streak = liquidations = 0
    for trade in trades:
        pnl = trade["net_pnl_usdt"]
        wins += pnl > 0
        gains += max(0, pnl)
        losses += max(0, -pnl)
        consecutive_losses = consecutive_losses + 1 if pnl < 0 else 0
        longest_streak = max(longest_streak, consecutive_losses)
        liquidations += trade["exit_reason"] == "liquidation"

    return {
        "metrics": {
            "final_equity_usdt": balance,
            "net_return_pct": (balance / initial_equity - 1) * 100,
            "mdd_pct": mdd * 100,
            "profit_factor": gains / losses if losses else None,
            "win_rate_pct": wins / len(trades) * 100 if trades else 0.0,
            "trade_count": len(trades),
            "max_consecutive_losses": longest_streak,
            "liquidation_occurred": liquidations > 0,
            "liquidation_count": liquidations,
            "max_entry_exposure_pct": max_entry_exposure_pct,
            "daily_halt_count": daily_halts,
            "peak_halt_time": peak_halt_time,
        },
        "trades": trades,
        "equity_curve": curve,
    }


def candle_checksum(candles):
    return sha256(candles.to_csv(float_format="%.17g").encode()).hexdigest()
