"""Long/cash BTCUSDT strategies with monthly decisions and minute execution."""

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class MonthlyRegime:
    lookback: int
    exposure: float

    def decisions(self, candles):
        # Label each completed UTC calendar month by the first minute of the next month.
        closes = candles.close.resample("MS").last()
        # If the final minute is absent, the archive cannot confirm that month's close.
        boundary = closes.index + pd.offsets.MonthBegin()
        closes = closes.where((boundary - pd.Timedelta(minutes=1)).isin(candles.index))
        average = closes.rolling(self.lookback).mean()
        valid = average.notna()
        return dict(zip(boundary[valid], closes[valid] > average[valid]))


REGIMES = {
    "SAFE_1X_AGGRESSIVE_V2": MonthlyRegime(4, 0.90),
    "SAFE_1X_BALANCED_V2": MonthlyRegime(8, 0.75),
    "SAFE_1X_DEFENSIVE_V2": MonthlyRegime(12, 0.55),
}


def replay_regime(candles, decisions, exposure, initial, fee_bps, slippage_bps, funding, *, curve=True):
    """Apply monthly long/cash decisions at the open and mark NAV each minute."""
    if not 0 < exposure < 1:
        raise ValueError("롱 전략 진입 비중은 0과 1 사이여야 합니다.")
    index = candles.index
    fee_rate, slip = fee_bps / 10_000, slippage_bps / 10_000
    closes, opens = candles.close.to_numpy(), candles.open.to_numpy()
    equity = np.full(len(candles), initial, dtype=float) if curve else None
    realized, peak, mdd = initial, initial, 0.0
    quantity = 0.0
    trades = []
    payments = {t: price * rate for t, (price, rate) in funding.items() if t in index}
    positions = index.get_indexer(sorted(set(decisions) | set(payments) | {index[-1]}))
    positions = positions[positions >= 0]

    def mark(values):
        nonlocal peak, mdd
        if len(values):
            peaks = np.maximum.accumulate(np.maximum(values, peak))
            mdd = max(mdd, float(np.max(1 - values / peaks)))
            peak = peaks[-1]

    cursor = 0
    for i in positions:
        time = index[i]
        # Cash stays constant between trades or funding marks.
        if quantity:
            segment = realized + quantity * (closes[cursor:i] - entry)
            mark(segment)
            if curve:
                equity[cursor:i] = segment
        elif curve:
            equity[cursor:i] = realized

        desired = decisions.get(time)
        if quantity:
            cost = quantity * payments.get(time, 0.0)
            funding_cost += cost
            realized -= cost
            # Positive residual cash keeps long notional at or below marked NAV.
            needs_exit = realized <= quantity * entry
            if (desired is not None and not desired) or needs_exit or i == len(index) - 1:
                at_end = i == len(index) - 1 and desired is not False and not needs_exit
                price = (closes[i] if at_end else opens[i]) * (1 - slip)
                exit_fee = quantity * price * fee_rate
                gross = quantity * (price - entry)
                realized += gross - exit_fee
                trades.append({
                    "entry_time": entry_time.isoformat() + "Z",
                    "exit_time": (time + pd.Timedelta(minutes=1) if at_end else time).isoformat() + "Z",
                    "side": "LONG", "entry_price": entry, "exit_price": price,
                    "quantity": quantity, "fees_usdt": entry_fee + exit_fee,
                    "funding_cost_usdt": funding_cost,
                    "net_pnl_usdt": gross - entry_fee - exit_fee - funding_cost,
                    "exit_reason": "exposure_limit" if needs_exit else "end_of_period" if at_end else "regime_exit",
                })
                quantity = 0.0
        elif desired and i < len(index) - 1 and realized > 0:
            entry_time, entry = time, opens[i] * (1 + slip)
            quantity = realized * exposure / (entry * (1 + fee_rate))
            entry_fee = quantity * entry * fee_rate
            realized -= entry_fee
            funding_cost = 0.0

        nav = realized + (quantity * (closes[i] - entry) if quantity else 0.0)
        mark([nav])
        if curve:
            equity[i] = nav
        cursor = i + 1

    pnl = [trade["net_pnl_usdt"] for trade in trades]
    loss = -sum(min(value, 0) for value in pnl)
    longest = streak = 0
    for value in pnl:
        streak = streak + 1 if value < 0 else 0
        longest = max(longest, streak)
    return {
        "metrics": {
            "final_equity_usdt": realized, "net_return_pct": (realized / initial - 1) * 100,
            "mdd_pct": mdd * 100,
            "profit_factor": sum(max(value, 0) for value in pnl) / loss if loss else None,
            "win_rate_pct": 100 * sum(value > 0 for value in pnl) / len(pnl) if pnl else 0.0,
            "trade_count": len(pnl), "max_consecutive_losses": longest,
            "liquidation_occurred": False, "liquidation_count": 0,
        },
        "trades": trades,
        "equity_curve": (
            [[index[0].isoformat() + "Z", initial]]
            + [[t.isoformat() + "Z", float(value)]
               for t, value in zip(index + pd.Timedelta(minutes=1), equity)]
            if curve else []
        ),
    }
