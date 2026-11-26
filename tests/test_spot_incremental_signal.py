import numpy as np
import pandas as pd
from pandas.testing import assert_frame_equal

from app.strategy import strategy_v57 as v57


def test_incremental_spot_buckets_features_and_signal_match_full(monkeypatch):
    index = pd.date_range("2025-01-01", periods=5500, freq="min", name="timestamp")
    x = np.arange(len(index))
    price = 100 + x / 100 + 5 * np.sin(x / 41)
    candles = pd.DataFrame({
        "open": price, "high": price + 1, "low": price - 1,
        "close": price + .2, "volume": 1 + x % 7,
    }, index=index)
    cache = v57.ClosedCandleAggregation()
    first = candles.iloc[:5000]
    v57.prepare_closed_candles(first, aggregation=cache)
    batches = [candles.iloc[11:5100], candles.iloc[11:5299].drop(index[5140]), candles.iloc[11:]]
    resample = v57.resample_ohlcv
    for position, batch in enumerate(batches):
        expected = v57.prepare_closed_candles(batch, legacy_alignment=True)
        calls = []

        def tracked(frame, rule):
            calls.append(len(frame))
            return resample(frame, rule)

        with monkeypatch.context() as patch:
            patch.setattr(v57, "resample_ohlcv", tracked)
            actual = v57.prepare_closed_candles(batch, legacy_alignment=True, aggregation=cache)
        if position == 0:
            assert max(calls) < len(batch)
        for computed, reference in zip(actual, expected):
            assert_frame_equal(computed, reference, check_freq=False)
        now = batch.index[-1] + pd.Timedelta(minutes=1)
        raw = batch.reset_index()
        assert v57.get_latest_v57_signal(raw, now, aggregation=cache) == v57.get_latest_v57_signal(raw, now)


def test_indicators_are_reused_until_a_bucket_closes(monkeypatch):
    candles = pd.DataFrame(
        {column: 1.0 for column in ["open", "high", "low", "close", "volume"]},
        index=pd.date_range("2025-01-01", periods=5000, freq="min"),
    )
    cache = v57.ClosedCandleAggregation()
    expected = v57.prepare_closed_candles(candles.iloc[:4999], aggregation=cache)

    def unexpected(*args, **kwargs):
        raise AssertionError("unchanged closed buckets must reuse the indicators")

    monkeypatch.setattr(v57, "add_common_indicators", unexpected)
    actual = v57.prepare_closed_candles(candles, aggregation=cache)
    for result, original in zip(actual, expected):
        assert_frame_equal(result, original, check_freq=False)
