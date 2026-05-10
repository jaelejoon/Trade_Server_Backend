import pandas as pd
import numpy as np
import re
import datetime

# ============================================================
# V57_LONG_SAFE_PUSH
#
# V56 기반
# - 청산 체크 유지
# - 청산 횟수 출력 유지
# - LONG 강화
# - LONG 손절 타이트
# - SHORT 신호 정제
#
# 목표:
# - 청산 0회 유지
# - LONG 수익성 강화
# - SHORT 품질 개선
# - MDD -10% 내외 목표
# ============================================================

INITIAL_CAPITAL = 10_000_000

FEE_RATE = 0.0005
SLIPPAGE = 0.0004

ENABLE_LIQUIDATION_CHECK = True
LIQUIDATION_LEVERAGE = 2.0
LIQUIDATION_BUFFER_PCT = 0.05

ATR_PERIOD = 14
EMA_FAST = 20
EMA_SLOW = 50
EMA_REGIME = 200
BB_PERIOD = 20
BB_STD = 2.0

MAX_OPEN_POSITIONS = 2
MAX_TOTAL_POSITION_RATIO = 1.50
MAX_TOTAL_RISK_RATIO = 0.08

COOLDOWN_BARS = 1

MIN_STOP_PCT = 0.0010
MAX_STOP_PCT = 0.050

ENABLE_BAD_HOUR_FILTER = True
BAD_ENTRY_HOURS_1H = [5, 12, 18, 19, 20, 23]

ENABLE_EVENT_FILTER = True
BLOCK_ENTRY_ON_EXTREME_VOLATILITY = True
EVENT_RISK_REDUCE_POSITION = True
EVENT_RISK_REDUCTION_MULT = 0.50

ATR_SPIKE_LOOKBACK = 48
ATR_SPIKE_MULT = 2.2
ADX_CHANGE_LOOKBACK = 6
ADX_SPIKE_THRESHOLD = 16
EXTREME_ATR_PCT_1H = 0.030
EXTREME_BB_WIDTH_1H = 0.180
EVENT_COOLDOWN_BARS = 4

ENABLE_TRAILING_STOP = True
TRAIL_START_R = 1.05
TRAIL_ATR_MULT = 2.00
TRAIL_LOCK_R = 0.25

ENABLE_EMA_EXIT = True
EMA_EXIT_AFTER_R = 1.80

ENABLE_EQUITY_GUARD = True
DAILY_DD_STOP_PCT = -0.04
MONTHLY_DD_STOP_PCT = -0.10
PEAK_DD_STOP_PCT = -0.18

USE_B_BREAKOUT_LONG = True
USE_SHORT_CRASH = True

# ============================================================
# B LONG - V57 강화
# ============================================================

B_RSI_MIN = 55
B_RSI_MAX = 81
B_ATR_PCT_MIN = 0.0045
B_BB_WIDTH_MIN = 0.026

# V56: 1.12 → V57: 1.00
B_STOP_ATR_MULT = 1.00

B_TAKE_PROFIT_R = 8.50

# V56: 0.035 / 0.025 → V57: 0.045 / 0.030
B_RISK_STRONG = 0.045
B_RISK_NORMAL = 0.030

# V56: 0.75 / 0.55 → V57: 0.85 / 0.60
B_MAX_POS_STRONG = 0.85
B_MAX_POS_NORMAL = 0.60
B_MAX_HOLD_BARS = 96

# ============================================================
# SHORT - V57 정제
# ============================================================

S_RSI_MIN = 22

# V56: 51 → V57: 45
S_RSI_MAX = 45

S_ATR_PCT_MIN = 0.005
S_BB_WIDTH_MIN = 0.020
S_STOP_ATR_MULT = 1.22
S_TAKE_PROFIT_R = 5.50

S_RISK = 0.012
S_MAX_POS = 0.25
S_MAX_HOLD_BARS = 72


def days_from_civil(y, m, d):
    y -= m <= 2
    era = (y if y >= 0 else y - 399) // 400
    yoe = y - era * 400
    doy = (153 * (m + (-3 if m > 2 else 9)) + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def recover_wrong_timestamp(value):
    try:
        s = str(value).strip()
        match = re.match(
            r"(\d+)-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{1,2}):(\d{1,2})",
            s
        )

        if not match:
            return pd.NaT

        y, m, d, hh, mm, ss = map(int, match.groups())

        if y < 3000:
            return pd.to_datetime(s, errors="coerce")

        wrong_seconds = days_from_civil(y, m, d) * 86400 + hh * 3600 + mm * 60 + ss
        actual_seconds = wrong_seconds / 1000

        return datetime.datetime.fromtimestamp(
            actual_seconds,
            datetime.UTC
        ).replace(tzinfo=None)

    except Exception:
        return pd.NaT


def parse_single_timestamp(value):
    try:
        if pd.isna(value):
            return pd.NaT

        if isinstance(value, (int, float, np.integer, np.floating)):
            v = float(value)

            if v > 1e15:
                return pd.to_datetime(v, unit="us", errors="coerce")
            if v > 1e12:
                return pd.to_datetime(v, unit="ms", errors="coerce")
            if v > 1e9:
                return pd.to_datetime(v, unit="s", errors="coerce")

            return pd.NaT

        s = str(value).strip()

        if s == "":
            return pd.NaT

        if re.fullmatch(r"\d+(\.\d+)?", s):
            v = float(s)

            if v > 1e15:
                return pd.to_datetime(v, unit="us", errors="coerce")
            if v > 1e12:
                return pd.to_datetime(v, unit="ms", errors="coerce")
            if v > 1e9:
                return pd.to_datetime(v, unit="s", errors="coerce")

            return pd.NaT

        if re.match(r"^\d{5,}-", s):
            return recover_wrong_timestamp(s)

        if "AM" in s.upper() or "PM" in s.upper():
            dt = pd.to_datetime(s, format="%Y-%m-%d %I:%M:%S %p", errors="coerce")
            if pd.notna(dt):
                return dt

        dt = pd.to_datetime(s, errors="coerce")

        if pd.notna(dt):
            return dt

        return recover_wrong_timestamp(s)

    except Exception:
        return pd.NaT


def parse_timestamp_column(df):
    df = df.copy()

    print()
    print("[Timestamp 파싱 시작]")

    before = len(df)
    df["timestamp"] = df["timestamp"].apply(parse_single_timestamp)
    failed_count = df["timestamp"].isna().sum()
    df = df.dropna(subset=["timestamp"])
    after = len(df)

    print(f"전체 행 수: {before}")
    print(f"유효 timestamp 수: {after}")
    print(f"timestamp 파싱 실패 수: {failed_count}")

    if after > 0:
        print("timestamp 변환 확인:")
        print(df["timestamp"].head())
        print(df["timestamp"].tail())

    return df


def rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    result = 100 - (100 / (1 + rs))

    return result.fillna(50)


def atr(df, period=14):
    prev_close = df["close"].shift(1)

    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs()
    ], axis=1).max(axis=1)

    return tr.ewm(alpha=1 / period, adjust=False).mean()


def adx(df, period=14):
    high = df["high"]
    low = df["low"]

    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0)

    tr = atr(df, period).replace(0, np.nan)

    plus_di = (
            100
            * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean()
            / tr
    )

    minus_di = (
            100
            * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean()
            / tr
    )

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)

    return dx.ewm(alpha=1 / period, adjust=False).mean().fillna(0)


def resample_ohlcv(df, rule):
    return df.resample(rule).agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum"
    }).dropna()


def add_common_indicators(df, prefix=""):
    out = df.copy()

    out[f"{prefix}atr"] = atr(out, ATR_PERIOD)
    out[f"{prefix}atr_pct"] = out[f"{prefix}atr"] / out["close"]
    out[f"{prefix}rsi"] = rsi(out["close"], 14)

    out[f"{prefix}ema_fast"] = out["close"].ewm(span=EMA_FAST, adjust=False).mean()
    out[f"{prefix}ema_slow"] = out["close"].ewm(span=EMA_SLOW, adjust=False).mean()
    out[f"{prefix}ema_regime"] = out["close"].ewm(span=EMA_REGIME, adjust=False).mean()

    out[f"{prefix}prev_close"] = out["close"].shift(1)
    out[f"{prefix}prev_open"] = out["open"].shift(1)

    out[f"{prefix}bb_mid"] = out["close"].rolling(BB_PERIOD).mean()
    out[f"{prefix}bb_std"] = out["close"].rolling(BB_PERIOD).std()
    out[f"{prefix}bb_upper"] = out[f"{prefix}bb_mid"] + out[f"{prefix}bb_std"] * BB_STD
    out[f"{prefix}bb_lower"] = out[f"{prefix}bb_mid"] - out[f"{prefix}bb_std"] * BB_STD
    out[f"{prefix}bb_width"] = (out[f"{prefix}bb_upper"] - out[f"{prefix}bb_lower"]) / out["close"]

    return out


def calc_liquidation_price(entry_price, side):
    if not ENABLE_LIQUIDATION_CHECK:
        return np.nan

    if LIQUIDATION_LEVERAGE <= 1:
        return np.nan

    if side == "LONG":
        return entry_price * (1 - (1 / LIQUIDATION_LEVERAGE) + LIQUIDATION_BUFFER_PCT)

    return entry_price * (1 + (1 / LIQUIDATION_LEVERAGE) - LIQUIDATION_BUFFER_PCT)


def prepare_data(raw_df):
    df = raw_df.copy()

    required_cols = ["timestamp", "open", "high", "low", "close", "volume"]

    for col in required_cols:
        if col not in df.columns:
            raise ValueError(f"CSV에 '{col}' 컬럼이 없습니다.")

    df = parse_timestamp_column(df)

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["open", "high", "low", "close", "volume"])
    df = df.sort_values("timestamp")
    df = df.drop_duplicates(subset=["timestamp"])

    print()
    print("정렬/중복제거 후 기간:")
    print(df["timestamp"].min(), "~", df["timestamp"].max())
    print(f"정렬/중복제거 후 데이터 수: {len(df)}")

    df = df.set_index("timestamp")

    df_15m = resample_ohlcv(df, "15min")
    df_1h = resample_ohlcv(df, "1h")
    df_4h = resample_ohlcv(df, "4h")

    df_15m = add_common_indicators(df_15m, prefix="m15_")
    df_1h = add_common_indicators(df_1h, prefix="")
    df_4h = add_common_indicators(df_4h, prefix="h4_")

    df_4h["h4_adx"] = adx(df_4h, 14)

    h4_features = df_4h[[
        "h4_adx",
        "h4_ema_fast",
        "h4_ema_slow",
        "h4_ema_regime"
    ]].shift(1)

    df_1h["atr_pct_ma"] = df_1h["atr_pct"].rolling(ATR_SPIKE_LOOKBACK).mean()

    df_1h = pd.merge_asof(
        df_1h.sort_index(),
        h4_features.sort_index(),
        left_index=True,
        right_index=True,
        direction="backward"
    )

    df_1h["h4_adx_change"] = df_1h["h4_adx"] - df_1h["h4_adx"].shift(ADX_CHANGE_LOOKBACK)

    df_1h["atr_spike"] = (
            (df_1h["atr_pct_ma"].notna()) &
            (df_1h["atr_pct"] > df_1h["atr_pct_ma"] * ATR_SPIKE_MULT)
    )

    df_1h["adx_spike"] = df_1h["h4_adx_change"].abs() >= ADX_SPIKE_THRESHOLD

    df_1h["extreme_volatility"] = (
            (df_1h["atr_pct"] >= EXTREME_ATR_PCT_1H) |
            (df_1h["bb_width"] >= EXTREME_BB_WIDTH_1H)
    )

    df_1h["raw_event_risk"] = (
            df_1h["atr_spike"] |
            df_1h["adx_spike"] |
            df_1h["extreme_volatility"]
    )

    df_1h["event_risk"] = (
        df_1h["raw_event_risk"]
        .rolling(EVENT_COOLDOWN_BARS, min_periods=1)
        .max()
        .astype(bool)
    )

    df_1h["market_bull"] = (
            (df_1h["close"] > df_1h["ema_regime"]) &
            (df_1h["ema_fast"] > df_1h["ema_slow"])
    )

    df_1h["h4_bull"] = (
            (df_1h["h4_ema_fast"] > df_1h["h4_ema_slow"]) &
            (df_1h["h4_ema_slow"] > df_1h["h4_ema_regime"])
    )

    df_1h["market_strong_bull"] = (
            df_1h["market_bull"] &
            df_1h["h4_bull"] &
            (df_1h["h4_adx"] >= 24)
    )

    df_1h["market_normal_bull"] = (
            df_1h["market_bull"] &
            (~df_1h["market_strong_bull"])
    )

    df_1h["market_bear"] = (
            (df_1h["close"] < df_1h["ema_regime"]) &
            (df_1h["ema_fast"] < df_1h["ema_slow"])
    )

    df_1h["market_regime"] = np.where(
        df_1h["market_strong_bull"],
        "STRONG_BULL",
        np.where(
            df_1h["market_normal_bull"],
            "NORMAL_BULL",
            np.where(df_1h["market_bear"], "BEAR", "MIXED")
        )
    )

    df_1h["bull_entry_allowed"] = df_1h["market_regime"].isin(["STRONG_BULL", "NORMAL_BULL"])
    df_1h["bad_entry_hour_1h"] = df_1h.index.hour.isin(BAD_ENTRY_HOURS_1H)

    df_1h["signal_b_breakout_long_raw"] = (
            USE_B_BREAKOUT_LONG &
            df_1h["bull_entry_allowed"] &
            (df_1h["close"] > df_1h["bb_upper"] * 0.995) &
            (df_1h["close"] > df_1h["open"]) &
            (df_1h["close"] > df_1h["prev_close"]) &
            (df_1h["ema_fast"] > df_1h["ema_slow"]) &
            (df_1h["rsi"] >= B_RSI_MIN) &
            (df_1h["rsi"] <= B_RSI_MAX) &
            (df_1h["atr_pct"] >= B_ATR_PCT_MIN) &
            (df_1h["bb_width"] >= B_BB_WIDTH_MIN)
    )

    df_1h["signal_short_crash_raw"] = (
            USE_SHORT_CRASH &
            (df_1h["market_regime"] == "BEAR") &
            (df_1h["close"] < df_1h["ema_fast"]) &
            (df_1h["close"] < df_1h["open"]) &
            (df_1h["close"] < df_1h["prev_close"]) &
            (df_1h["rsi"] >= S_RSI_MIN) &
            (df_1h["rsi"] <= S_RSI_MAX) &
            (df_1h["atr_pct"] >= S_ATR_PCT_MIN) &
            (df_1h["bb_width"] >= S_BB_WIDTH_MIN)
    )

    if ENABLE_EVENT_FILTER and BLOCK_ENTRY_ON_EXTREME_VOLATILITY:
        df_1h["signal_b_breakout_long"] = df_1h["signal_b_breakout_long_raw"] & (~df_1h["extreme_volatility"])
        df_1h["signal_short_crash"] = df_1h["signal_short_crash_raw"] & (~df_1h["extreme_volatility"])
    else:
        df_1h["signal_b_breakout_long"] = df_1h["signal_b_breakout_long_raw"]
        df_1h["signal_short_crash"] = df_1h["signal_short_crash_raw"]

    if ENABLE_BAD_HOUR_FILTER:
        df_1h["signal_b_breakout_long"] &= ~df_1h["bad_entry_hour_1h"]
        df_1h["signal_short_crash"] &= ~df_1h["bad_entry_hour_1h"]

    df_1h["signal_strategy_1h"] = np.where(
        df_1h["signal_b_breakout_long"],
        "B_1H_BREAKOUT_LONG",
        np.where(
            df_1h["signal_short_crash"],
            "S_SHORT_CRASH",
            "NONE"
        )
    )

    df_1h["entry_signal_1h"] = df_1h["signal_strategy_1h"] != "NONE"

    h1_context = df_1h[[
        "market_regime",
        "bull_entry_allowed",
        "event_risk",
        "extreme_volatility",
        "h4_adx",
        "ema_fast",
        "ema_slow",
        "ema_regime",
        "atr_pct",
        "bb_width"
    ]].shift(1)

    h1_context = h1_context.rename(columns={
        "market_regime": "h1_market_regime",
        "bull_entry_allowed": "h1_bull_entry_allowed",
        "event_risk": "h1_event_risk",
        "extreme_volatility": "h1_extreme_volatility",
        "h4_adx": "h1_h4_adx",
        "ema_fast": "h1_ema_fast",
        "ema_slow": "h1_ema_slow",
        "ema_regime": "h1_ema_regime",
        "atr_pct": "h1_atr_pct",
        "bb_width": "h1_bb_width"
    })

    df_15m = pd.merge_asof(
        df_15m.sort_index(),
        h1_context.sort_index(),
        left_index=True,
        right_index=True,
        direction="backward"
    )

    df_15m["signal_strategy_15m"] = "NONE"
    df_15m["entry_signal_15m"] = False

    print_signal_diagnostics(df_1h, df_15m, df_4h)

    return df_1h.dropna(), df_15m.dropna()


def print_signal_diagnostics(df_1h, df_15m, df_4h):
    print()
    print("=" * 80)
    print("진입 조건별 개수")
    print("=" * 80)
    print(f"1시간봉 전체: {len(df_1h)}")
    print(f"15분봉 전체: {len(df_15m)}")

    print()
    print("[1H 전략별 신호]")
    print(f"B_1H_BREAKOUT_LONG raw/final: {int(df_1h['signal_b_breakout_long_raw'].sum())} / {int(df_1h['signal_b_breakout_long'].sum())}")
    print(f"S_SHORT_CRASH raw/final: {int(df_1h['signal_short_crash_raw'].sum())} / {int(df_1h['signal_short_crash'].sum())}")

    print()
    print("[최종 신호]")
    print(f"1H TOTAL signal: {int(df_1h['entry_signal_1h'].sum())}")
    print(f"15M TOTAL signal: {int(df_15m['entry_signal_15m'].sum())}")
    print(f"ALL signal: {int(df_1h['entry_signal_1h'].sum()) + int(df_15m['entry_signal_15m'].sum())}")

    print()
    print("1H 최종 전략 신호 개수:")
    print(df_1h["signal_strategy_1h"].value_counts())

    print()
    print("4H ADX 통계:")
    print(df_4h["h4_adx"].describe())


def get_strategy_side(strategy):
    if strategy == "S_SHORT_CRASH":
        return "SHORT"
    return "LONG"


def get_strategy_config(strategy, market_regime, event_risk):
    if strategy == "B_1H_BREAKOUT_LONG":
        risk = B_RISK_STRONG if market_regime == "STRONG_BULL" else B_RISK_NORMAL
        max_pos = B_MAX_POS_STRONG if market_regime == "STRONG_BULL" else B_MAX_POS_NORMAL

        config = {
            "risk_per_trade": risk,
            "max_position_ratio": max_pos,
            "stop_atr_mult": B_STOP_ATR_MULT,
            "take_profit_r": B_TAKE_PROFIT_R,
            "max_hold_bars": B_MAX_HOLD_BARS
        }

    elif strategy == "S_SHORT_CRASH":
        config = {
            "risk_per_trade": S_RISK,
            "max_position_ratio": S_MAX_POS,
            "stop_atr_mult": S_STOP_ATR_MULT,
            "take_profit_r": S_TAKE_PROFIT_R,
            "max_hold_bars": S_MAX_HOLD_BARS
        }

    else:
        return None

    config["original_risk_per_trade"] = config["risk_per_trade"]

    if EVENT_RISK_REDUCE_POSITION and event_risk:
        config["risk_per_trade"] *= EVENT_RISK_REDUCTION_MULT
        config["max_position_ratio"] *= EVENT_RISK_REDUCTION_MULT

    return config


def calc_unrealized_pnl(positions, close):
    total = 0

    for position in positions:
        if position["side"] == "LONG":
            total += position["qty"] * (close - position["entry_price"])
        else:
            total += position["qty"] * (position["entry_price"] - close)

    return total


def calc_open_position_value(positions):
    return sum(position["position_value"] for position in positions)


def calc_open_risk_amount(positions):
    return sum(position["initial_risk_amount"] for position in positions)


def build_signal_events(df_1h, df_15m):
    events = []

    for time, row in df_1h.iterrows():
        if bool(row["entry_signal_1h"]):
            strategy = row["signal_strategy_1h"]
            events.append({
                "time": time,
                "timeframe": "1H",
                "side": get_strategy_side(strategy),
                "strategy": strategy,
                "market_regime": row["market_regime"],
                "event_risk": bool(row["event_risk"]),
                "atr": row["atr"],
                "rsi": row["rsi"],
                "h4_adx": row["h4_adx"],
                "bb_width": row["bb_width"],
                "atr_pct": row["atr_pct"]
            })

    events_df = pd.DataFrame(events)

    if events_df.empty:
        return events_df

    return events_df.sort_values("time").reset_index(drop=True)


def update_trailing_stop(position, row):
    if not ENABLE_TRAILING_STOP:
        return

    side = position["side"]
    entry_price = position["entry_price"]
    initial_risk = position["initial_risk"]

    if position["max_favorable_r"] < TRAIL_START_R:
        return

    atr_value = row["m15_atr"]

    if side == "LONG":
        lock_stop = entry_price + initial_risk * TRAIL_LOCK_R
        atr_stop = row["close"] - atr_value * TRAIL_ATR_MULT
        new_stop = max(position["stop_price"], lock_stop, atr_stop)

        if new_stop < row["close"]:
            position["stop_price"] = new_stop
            position["trailing_stop_activated"] = True

    else:
        lock_stop = entry_price - initial_risk * TRAIL_LOCK_R
        atr_stop = row["close"] + atr_value * TRAIL_ATR_MULT
        new_stop = min(position["stop_price"], lock_stop, atr_stop)

        if new_stop > row["close"]:
            position["stop_price"] = new_stop
            position["trailing_stop_activated"] = True


def should_block_new_entry(equity, equity_peak, day_start_equity, month_start_equity):
    if not ENABLE_EQUITY_GUARD:
        return False, ""

    if equity_peak > 0:
        peak_dd = equity / equity_peak - 1
        if peak_dd <= PEAK_DD_STOP_PCT:
            return True, "PEAK_DD_GUARD"

    if day_start_equity > 0:
        day_dd = equity / day_start_equity - 1
        if day_dd <= DAILY_DD_STOP_PCT:
            return True, "DAILY_DD_GUARD"

    if month_start_equity > 0:
        month_dd = equity / month_start_equity - 1
        if month_dd <= MONTHLY_DD_STOP_PCT:
            return True, "MONTHLY_DD_GUARD"

    return False, ""


def backtest(df_15m, signal_events):
    balance = INITIAL_CAPITAL
    equity_curve = []
    trades = []

    positions = []
    cooldown = 0
    trade_id_seq = 1

    equity_peak = INITIAL_CAPITAL
    current_day = None
    current_month = None
    day_start_equity = INITIAL_CAPITAL
    month_start_equity = INITIAL_CAPITAL
    guard_block_count = 0
    guard_last_reason = ""
    liquidation_count = 0

    if signal_events.empty:
        signal_events_by_time = {}
    else:
        signal_events_by_time = {
            k: v.to_dict("records")
            for k, v in signal_events.groupby("time")
        }

    for i in range(1, len(df_15m)):
        row = df_15m.iloc[i]
        time = df_15m.index[i]

        open_price = row["open"]
        close = row["close"]
        high = row["high"]
        low = row["low"]

        unrealized_before = calc_unrealized_pnl(positions, close)
        current_equity_before = balance + unrealized_before

        date_key = time.date()
        month_key = time.to_period("M")

        if current_day != date_key:
            current_day = date_key
            day_start_equity = current_equity_before

        if current_month != month_key:
            current_month = month_key
            month_start_equity = current_equity_before

        equity_peak = max(equity_peak, current_equity_before)

        if cooldown > 0:
            cooldown -= 1

        remaining_positions = []

        for position in positions:
            position["bars_held"] += 1

            entry_price = position["entry_price"]
            qty = position["qty"]
            side = position["side"]

            if side == "LONG":
                current_favorable_r = (high - entry_price) / position["initial_risk"]
                current_adverse_r = (low - entry_price) / position["initial_risk"]
            else:
                current_favorable_r = (entry_price - low) / position["initial_risk"]
                current_adverse_r = (entry_price - high) / position["initial_risk"]

            position["max_favorable_r"] = max(position["max_favorable_r"], current_favorable_r)
            position["max_adverse_r"] = min(position["max_adverse_r"], current_adverse_r)

            update_trailing_stop(position, row)

            exit_reason = None
            exit_price = None

            if ENABLE_LIQUIDATION_CHECK:
                if side == "LONG" and low <= position["liquidation_price"]:
                    exit_reason = "LIQUIDATION"
                    exit_price = position["liquidation_price"]
                    liquidation_count += 1

                elif side == "SHORT" and high >= position["liquidation_price"]:
                    exit_reason = "LIQUIDATION"
                    exit_price = position["liquidation_price"]
                    liquidation_count += 1

            if exit_reason is None:
                if side == "LONG":
                    if low <= position["stop_price"]:
                        exit_reason = "TRAIL_STOP" if position.get("trailing_stop_activated", False) else "STOP"
                        exit_price = position["stop_price"]

                    elif high >= position["take_profit_price"]:
                        exit_reason = "BIG_TAKE_PROFIT"
                        exit_price = position["take_profit_price"]

                    elif ENABLE_EMA_EXIT and position["max_favorable_r"] >= EMA_EXIT_AFTER_R:
                        if close < row["m15_ema_slow"]:
                            exit_reason = "EMA_TREND_EXIT"
                            exit_price = close

                else:
                    if high >= position["stop_price"]:
                        exit_reason = "TRAIL_STOP" if position.get("trailing_stop_activated", False) else "STOP"
                        exit_price = position["stop_price"]

                    elif low <= position["take_profit_price"]:
                        exit_reason = "BIG_TAKE_PROFIT"
                        exit_price = position["take_profit_price"]

                    elif ENABLE_EMA_EXIT and position["max_favorable_r"] >= EMA_EXIT_AFTER_R:
                        if close > row["m15_ema_slow"]:
                            exit_reason = "EMA_TREND_EXIT"
                            exit_price = close

            if exit_reason is None and position["bars_held"] >= position["max_hold_bars"]:
                exit_reason = "TIME_EXIT"
                exit_price = close

            if exit_reason is not None:
                if side == "LONG":
                    exit_price = exit_price * (1 - SLIPPAGE)
                    gross_pnl = qty * (exit_price - entry_price)
                else:
                    exit_price = exit_price * (1 + SLIPPAGE)
                    gross_pnl = qty * (entry_price - exit_price)

                exit_fee = qty * exit_price * FEE_RATE
                total_pnl = position["realized_pnl"] + gross_pnl - exit_fee

                balance += gross_pnl - exit_fee

                trades.append({
                    "trade_id": position["trade_id"],
                    "side": side,
                    "timeframe": position["timeframe"],
                    "strategy": position["strategy"],
                    "market_regime": position["market_regime"],
                    "risk_per_trade": position["risk_per_trade"],
                    "original_risk_per_trade": position["original_risk_per_trade"],
                    "max_position_ratio": position["max_position_ratio"],
                    "leverage": position["leverage"],
                    "liquidation_price": position["liquidation_price"],
                    "entry_time": position["entry_time"],
                    "exit_time": time,
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "stop_price": position["stop_price"],
                    "take_profit_price": position["take_profit_price"],
                    "qty": qty,
                    "position_value": position["position_value"],
                    "entry_fee": position["entry_fee"],
                    "exit_fee": exit_fee,
                    "pnl": total_pnl,
                    "r_multiple": total_pnl / position["initial_risk_amount"] if position["initial_risk_amount"] > 0 else 0,
                    "exit_reason": exit_reason,
                    "bars_held": position["bars_held"],
                    "max_favorable_r": position["max_favorable_r"],
                    "max_adverse_r": position["max_adverse_r"],
                    "trailing_stop_activated": position.get("trailing_stop_activated", False),
                    "entry_rsi": position["entry_rsi"],
                    "entry_h4_adx": position["entry_h4_adx"],
                    "entry_bb_width": position["entry_bb_width"],
                    "entry_atr_pct": position["entry_atr_pct"],
                    "entry_event_risk": position["entry_event_risk"],
                    "balance": balance
                })

                cooldown = COOLDOWN_BARS
            else:
                remaining_positions.append(position)

        positions = remaining_positions

        unrealized_after_exit = calc_unrealized_pnl(positions, close)
        current_equity_after_exit = balance + unrealized_after_exit
        equity_peak = max(equity_peak, current_equity_after_exit)

        entry_blocked, guard_reason = should_block_new_entry(
            current_equity_after_exit,
            equity_peak,
            day_start_equity,
            month_start_equity
        )

        if entry_blocked:
            guard_block_count += 1
            guard_last_reason = guard_reason

        if (not entry_blocked) and len(positions) < MAX_OPEN_POSITIONS and cooldown == 0:
            current_events = signal_events_by_time.get(time, [])

            if current_events:
                priority = {
                    "B_1H_BREAKOUT_LONG": 1,
                    "S_SHORT_CRASH": 2
                }

                sorted_signals = sorted(
                    current_events,
                    key=lambda x: priority.get(x["strategy"], 99)
                )

                for signal in sorted_signals:
                    if len(positions) >= MAX_OPEN_POSITIONS:
                        break

                    config = get_strategy_config(
                        signal["strategy"],
                        signal["market_regime"],
                        bool(signal["event_risk"])
                    )

                    if config is None:
                        continue

                    risk_per_trade = config["risk_per_trade"]
                    max_position_ratio = config["max_position_ratio"]

                    if risk_per_trade <= 0 or max_position_ratio <= 0:
                        continue

                    current_total_position_value = calc_open_position_value(positions)
                    current_total_risk_amount = calc_open_risk_amount(positions)

                    max_allowed_total_position_value = balance * MAX_TOTAL_POSITION_RATIO
                    max_allowed_total_risk_amount = balance * MAX_TOTAL_RISK_RATIO

                    remaining_position_value_capacity = max_allowed_total_position_value - current_total_position_value
                    remaining_risk_capacity = max_allowed_total_risk_amount - current_total_risk_amount

                    if remaining_position_value_capacity <= 0 or remaining_risk_capacity <= 0:
                        continue

                    side = signal["side"]

                    if side == "LONG":
                        entry_price = open_price * (1 + SLIPPAGE)
                        stop_price = entry_price - signal["atr"] * config["stop_atr_mult"]

                        if stop_price >= entry_price:
                            continue

                        risk_per_unit = entry_price - stop_price
                        take_profit_price = entry_price + risk_per_unit * config["take_profit_r"]

                    else:
                        entry_price = open_price * (1 - SLIPPAGE)
                        stop_price = entry_price + signal["atr"] * config["stop_atr_mult"]

                        if stop_price <= entry_price:
                            continue

                        risk_per_unit = stop_price - entry_price
                        take_profit_price = entry_price - risk_per_unit * config["take_profit_r"]

                    stop_pct = risk_per_unit / entry_price

                    if not (MIN_STOP_PCT <= stop_pct <= MAX_STOP_PCT):
                        continue

                    account_risk = min(balance * risk_per_trade, remaining_risk_capacity)
                    max_position_value = min(
                        balance * max_position_ratio,
                        remaining_position_value_capacity
                    )

                    qty = min(
                        account_risk / risk_per_unit,
                        max_position_value / entry_price
                    )

                    if qty <= 0:
                        continue

                    position_value = qty * entry_price
                    entry_fee = position_value * FEE_RATE

                    if balance - entry_fee <= 0:
                        continue

                    balance -= entry_fee

                    liquidation_price = calc_liquidation_price(entry_price, side)

                    position = {
                        "trade_id": trade_id_seq,
                        "side": side,
                        "timeframe": signal["timeframe"],
                        "strategy": signal["strategy"],
                        "market_regime": signal["market_regime"],
                        "risk_per_trade": risk_per_trade,
                        "original_risk_per_trade": config["original_risk_per_trade"],
                        "max_position_ratio": max_position_ratio,
                        "leverage": LIQUIDATION_LEVERAGE,
                        "liquidation_price": liquidation_price,
                        "entry_time": time,
                        "entry_price": entry_price,
                        "stop_price": stop_price,
                        "take_profit_price": take_profit_price,
                        "initial_risk": risk_per_unit,
                        "initial_risk_amount": risk_per_unit * qty,
                        "qty": qty,
                        "position_value": position_value,
                        "entry_fee": entry_fee,
                        "realized_pnl": -entry_fee,
                        "bars_held": 0,
                        "max_hold_bars": config["max_hold_bars"],
                        "max_favorable_r": 0,
                        "max_adverse_r": 0,
                        "trailing_stop_activated": False,
                        "entry_rsi": signal["rsi"],
                        "entry_h4_adx": signal["h4_adx"],
                        "entry_bb_width": signal["bb_width"],
                        "entry_atr_pct": signal["atr_pct"],
                        "entry_event_risk": bool(signal["event_risk"])
                    }

                    positions.append(position)
                    trade_id_seq += 1

        unrealized = calc_unrealized_pnl(positions, close)
        equity_now = balance + unrealized
        equity_peak = max(equity_peak, equity_now)

        equity_curve.append({
            "time": time,
            "equity": equity_now,
            "balance": balance,
            "unrealized_pnl": unrealized,
            "open_positions": len(positions),
            "open_position_value": calc_open_position_value(positions),
            "open_risk_amount": calc_open_risk_amount(positions),
            "guard_block_count": guard_block_count,
            "guard_last_reason": guard_last_reason,
            "liquidation_count": liquidation_count
        })

    equity_df = pd.DataFrame(equity_curve)

    if not equity_df.empty:
        equity_df = equity_df.set_index("time")

    trades_df = pd.DataFrame(trades)

    return equity_df, trades_df


def calculate_mdd_from_equity(equity_series):
    if equity_series is None or len(equity_series) == 0:
        return 0

    cummax = equity_series.cummax()
    drawdown = equity_series / cummax - 1

    return drawdown.min() * 100


def calculate_max_losing_streak(trades_df):
    max_streak = 0
    current = 0

    for pnl in trades_df["pnl"]:
        if pnl <= 0:
            current += 1
            max_streak = max(max_streak, current)
        else:
            current = 0

    return max_streak


def calculate_stats(trades_df, equity_df=None):
    if trades_df.empty:
        return {
            "trades": 0,
            "pnl": 0,
            "win_rate": 0,
            "avg_win": 0,
            "avg_loss": 0,
            "expectancy": 0,
            "avg_r": 0,
            "pf": 0,
            "mdd": 0,
            "return_pct": 0,
            "final_equity": INITIAL_CAPITAL,
            "failed_1r": 0,
            "max_losing_streak": 0,
            "worst_trade": 0,
            "avg_bars_held": 0,
            "liquidation_count": 0
        }

    wins = trades_df[trades_df["pnl"] > 0]
    losses = trades_df[trades_df["pnl"] <= 0]

    gross_profit = wins["pnl"].sum()
    gross_loss = abs(losses["pnl"].sum())

    failed_1r = trades_df[
        (trades_df["max_favorable_r"] >= 1.0) &
        (trades_df["pnl"] <= 0)
        ]

    liquidation_count = int((trades_df["exit_reason"] == "LIQUIDATION").sum())

    final_equity = INITIAL_CAPITAL + trades_df["pnl"].sum()
    return_pct = (final_equity / INITIAL_CAPITAL - 1) * 100

    if equity_df is not None and not equity_df.empty:
        final_equity = equity_df["equity"].iloc[-1]
        return_pct = (final_equity / INITIAL_CAPITAL - 1) * 100
        mdd = calculate_mdd_from_equity(equity_df["equity"])
    else:
        mdd = calculate_mdd_from_equity(INITIAL_CAPITAL + trades_df["pnl"].cumsum())

    return {
        "trades": len(trades_df),
        "pnl": trades_df["pnl"].sum(),
        "win_rate": len(wins) / len(trades_df) * 100,
        "avg_win": wins["pnl"].mean() if len(wins) > 0 else 0,
        "avg_loss": losses["pnl"].mean() if len(losses) > 0 else 0,
        "expectancy": trades_df["pnl"].mean(),
        "avg_r": trades_df["r_multiple"].mean(),
        "pf": gross_profit / gross_loss if gross_loss > 0 else np.inf,
        "mdd": mdd,
        "return_pct": return_pct,
        "final_equity": final_equity,
        "failed_1r": len(failed_1r),
        "max_losing_streak": calculate_max_losing_streak(trades_df),
        "worst_trade": trades_df["pnl"].min(),
        "avg_bars_held": trades_df["bars_held"].mean(),
        "liquidation_count": liquidation_count
    }


def print_group_stats(title, trades_df, group_col):
    if trades_df.empty or group_col not in trades_df.columns:
        return

    print()
    print("=" * 80)
    print(title)
    print("=" * 80)

    rows = []

    for key, group in trades_df.groupby(group_col, observed=False):
        st = calculate_stats(group)

        rows.append({
            group_col: key,
            "trades": st["trades"],
            "pnl": round(st["pnl"], 0),
            "return_on_initial_%": round(st["pnl"] / INITIAL_CAPITAL * 100, 2),
            "win_rate": round(st["win_rate"], 2),
            "expectancy": round(st["expectancy"], 0),
            "avg_r": round(st["avg_r"], 3),
            "pf": round(st["pf"], 2),
            "mdd_trade_based": round(st["mdd"], 2),
            "failed_1r": st["failed_1r"],
            "liquidation_count": st["liquidation_count"],
            "avg_bars_held": round(st["avg_bars_held"], 2)
        })

    result = pd.DataFrame(rows)

    if result.empty:
        print("데이터 없음")
    else:
        print(result.to_string(index=False))


def analyze_result(equity_df, trades_df, signal_events):
    print()
    print("=" * 80)
    print("백테스트 결과 - V57_LONG_SAFE_PUSH")
    print("=" * 80)

    stats = calculate_stats(trades_df, equity_df)

    print(f"최종 자산: {stats['final_equity']:,.0f}")
    print(f"총 수익률: {stats['return_pct']:.2f}%")
    print(f"목표 배수: {stats['final_equity'] / INITIAL_CAPITAL:.2f}배")
    print(f"MDD: {stats['mdd']:.2f}%")
    print(f"거래 수: {stats['trades']}")
    print(f"청산 횟수: {stats['liquidation_count']}")
    print(f"총 손익: {stats['pnl']:,.0f}")
    print(f"승률: {stats['win_rate']:.2f}%")
    print(f"평균 수익: {stats['avg_win']:,.0f}")
    print(f"평균 손실: {stats['avg_loss']:,.0f}")
    print(f"기대값: {stats['expectancy']:,.0f}")
    print(f"평균 R: {stats['avg_r']:.3f}")
    print(f"PF: {stats['pf']:.2f}")
    print(f"1R 이상 갔다가 손실 종료: {stats['failed_1r']}")
    print(f"최대 연속 손실: {stats['max_losing_streak']}")
    print(f"최악 거래 손실: {stats['worst_trade']:,.0f}")
    print(f"평균 보유 시간: {stats['avg_bars_held']:.2f} 봉")

    if equity_df is not None and not equity_df.empty:
        print()
        print("=" * 80)
        print("청산 방어용 노출 통계")
        print("=" * 80)
        print(f"최대 동시 포지션 수: {int(equity_df['open_positions'].max())}")
        print(f"평균 동시 포지션 수: {equity_df['open_positions'].mean():.2f}")
        print(f"최대 오픈 포지션 가치: {equity_df['open_position_value'].max():,.0f}")
        print(f"최대 오픈 리스크 금액: {equity_df['open_risk_amount'].max():,.0f}")
        print(f"방어 로직 발동 누적 횟수: {int(equity_df['guard_block_count'].max())}")
        print(f"마지막 방어 사유: {equity_df['guard_last_reason'].iloc[-1]}")

    if not signal_events.empty:
        print()
        print("=" * 80)
        print("신호 이벤트 개수")
        print("=" * 80)
        print(signal_events["strategy"].value_counts().to_string())

    if trades_df.empty:
        print("거래가 없습니다.")
        return

    trades_df = trades_df.copy()
    trades_df["entry_time"] = pd.to_datetime(trades_df["entry_time"])
    trades_df["exit_time"] = pd.to_datetime(trades_df["exit_time"])

    trades_df["month"] = trades_df["entry_time"].dt.to_period("M").astype(str)
    trades_df["entry_hour"] = trades_df["entry_time"].dt.hour

    monthly = trades_df.groupby("month")["pnl"].sum()

    print()
    print("=" * 80)
    print("월별 안정성")
    print("=" * 80)
    print(f"수익 월 수: {(monthly > 0).sum()}")
    print(f"손실 월 수: {(monthly <= 0).sum()}")
    print(f"최악 월 손익: {monthly.min():,.0f}")

    print_group_stats("롱/숏별 상세 성과", trades_df, "side")
    print_group_stats("전략별 상세 성과", trades_df, "strategy")
    print_group_stats("월별 성과", trades_df, "month")
    print_group_stats("시간대별 성과", trades_df, "entry_hour")
    print_group_stats("청산사유별 성과", trades_df, "exit_reason")


# ============================================================
# Server Signal Wrapper
# - 서버에서 V57을 호출하기 위한 래퍼
# - 위 전략 계산식은 변경하지 않는다.
# ============================================================

from dataclasses import dataclass
from typing import Optional, Dict, Any


@dataclass
class V57SignalResult:
    has_signal: bool
    signal_time: Optional[str]
    side: str
    binance_side: Optional[str]
    strategy: str
    market_regime: Optional[str]
    event_risk: bool
    atr: Optional[float]
    rsi: Optional[float]
    h4_adx: Optional[float]
    bb_width: Optional[float]
    atr_pct: Optional[float]
    risk_per_trade: Optional[float]
    original_risk_per_trade: Optional[float]
    max_position_ratio: Optional[float]
    stop_atr_mult: Optional[float]
    take_profit_r: Optional[float]
    max_hold_bars: Optional[int]
    reason: str


def empty_signal(reason: str) -> V57SignalResult:
    return V57SignalResult(
        has_signal=False,
        signal_time=None,
        side="NONE",
        binance_side=None,
        strategy="NONE",
        market_regime=None,
        event_risk=False,
        atr=None,
        rsi=None,
        h4_adx=None,
        bb_width=None,
        atr_pct=None,
        risk_per_trade=None,
        original_risk_per_trade=None,
        max_position_ratio=None,
        stop_atr_mult=None,
        take_profit_r=None,
        max_hold_bars=None,
        reason=reason,
    )


def _to_optional_float(value):
    try:
        if pd.isna(value):
            return None
        return float(value)
    except Exception:
        return None


def get_latest_v57_signal(raw_df: pd.DataFrame) -> V57SignalResult:
    if raw_df is None or raw_df.empty:
        return empty_signal("NO_RAW_DATA")

    try:
        df_1h, df_15m = prepare_data(raw_df)
    except Exception as exc:
        return empty_signal(f"PREPARE_DATA_ERROR: {exc}")

    if df_1h.empty:
        return empty_signal("NO_1H_DATA")

    if df_15m.empty:
        return empty_signal("NO_15M_DATA")

    signal_events = build_signal_events(df_1h, df_15m)

    if signal_events.empty:
        latest_1h = df_1h.iloc[-1]
        return V57SignalResult(
            has_signal=False,
            signal_time=None,
            side="NONE",
            binance_side=None,
            strategy="NONE",
            market_regime=str(latest_1h.get("market_regime")),
            event_risk=bool(latest_1h.get("event_risk", False)),
            atr=_to_optional_float(latest_1h.get("atr")),
            rsi=_to_optional_float(latest_1h.get("rsi")),
            h4_adx=_to_optional_float(latest_1h.get("h4_adx")),
            bb_width=_to_optional_float(latest_1h.get("bb_width")),
            atr_pct=_to_optional_float(latest_1h.get("atr_pct")),
            risk_per_trade=None,
            original_risk_per_trade=None,
            max_position_ratio=None,
            stop_atr_mult=None,
            take_profit_r=None,
            max_hold_bars=None,
            reason="NO_SIGNAL",
        )

    latest_signal = signal_events.iloc[-1]

    config = get_strategy_config(
        latest_signal["strategy"],
        latest_signal["market_regime"],
        bool(latest_signal["event_risk"]),
    )

    if config is None:
        return empty_signal("CONFIG_NOT_FOUND")

    side = latest_signal["side"]
    binance_side = "BUY" if side == "LONG" else "SELL"

    return V57SignalResult(
        has_signal=True,
        signal_time=str(latest_signal["time"]),
        side=side,
        binance_side=binance_side,
        strategy=latest_signal["strategy"],
        market_regime=latest_signal["market_regime"],
        event_risk=bool(latest_signal["event_risk"]),
        atr=float(latest_signal["atr"]),
        rsi=float(latest_signal["rsi"]),
        h4_adx=float(latest_signal["h4_adx"]),
        bb_width=float(latest_signal["bb_width"]),
        atr_pct=float(latest_signal["atr_pct"]),
        risk_per_trade=float(config["risk_per_trade"]),
        original_risk_per_trade=float(config["original_risk_per_trade"]),
        max_position_ratio=float(config["max_position_ratio"]),
        stop_atr_mult=float(config["stop_atr_mult"]),
        take_profit_r=float(config["take_profit_r"]),
        max_hold_bars=int(config["max_hold_bars"]),
        reason="SIGNAL_FOUND",
    )


def signal_to_dict(signal: V57SignalResult) -> Dict[str, Any]:
    return {
        "has_signal": signal.has_signal,
        "signal_time": signal.signal_time,
        "side": signal.side,
        "binance_side": signal.binance_side,
        "strategy": signal.strategy,
        "market_regime": signal.market_regime,
        "event_risk": signal.event_risk,
        "atr": signal.atr,
        "rsi": signal.rsi,
        "h4_adx": signal.h4_adx,
        "bb_width": signal.bb_width,
        "atr_pct": signal.atr_pct,
        "risk_per_trade": signal.risk_per_trade,
        "original_risk_per_trade": signal.original_risk_per_trade,
        "max_position_ratio": signal.max_position_ratio,
        "stop_atr_mult": signal.stop_atr_mult,
        "take_profit_r": signal.take_profit_r,
        "max_hold_bars": signal.max_hold_bars,
        "reason": signal.reason,
    }
