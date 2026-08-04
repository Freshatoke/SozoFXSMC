import pandas as pd

from config.settings import SwingConfig
from src.structure.swings import detect_swings
from tests.helpers import make_candles


def _rows_from_highs(highs):
    rows = []
    for h in highs:
        rows.append((h - 0.005, h, h - 0.01, h - 0.005))
    return rows


def _rows_from_lows(lows):
    rows = []
    for l in lows:
        rows.append((l + 0.005, l + 0.01, l, l + 0.005))
    return rows


def test_bullish_swing_high_detected():
    highs = [1.0, 1.1, 1.2, 1.5, 1.2, 1.1, 1.0]
    df = make_candles(_rows_from_highs(highs))
    cfg = SwingConfig(left=2, right=2)
    swings = detect_swings(df, config=cfg, timeframe_label="M1")
    high_swings = swings[swings.swing_type == "high"]
    assert len(high_swings) == 1
    row = high_swings.iloc[0]
    assert row["candle_index"] == 3
    assert row["price"] == 1.5
    assert row["swing_timestamp"] == df["timestamp"].iloc[3]


def test_bearish_swing_low_detected():
    lows = [1.0, 0.9, 0.8, 0.5, 0.8, 0.9, 1.0]
    df = make_candles(_rows_from_lows(lows))
    cfg = SwingConfig(left=2, right=2)
    swings = detect_swings(df, config=cfg, timeframe_label="M1")
    low_swings = swings[swings.swing_type == "low"]
    assert len(low_swings) == 1
    row = low_swings.iloc[0]
    assert row["candle_index"] == 3
    assert row["price"] == 0.5


def test_swing_not_usable_before_confirmation_timestamp():
    highs = [1.0, 1.1, 1.2, 1.5, 1.2, 1.1, 1.0]
    df = make_candles(_rows_from_highs(highs))
    cfg = SwingConfig(left=2, right=2)
    swings = detect_swings(df, config=cfg, timeframe_label="M1")
    row = swings.iloc[0]

    # The swing occurs at candle index 3 but is not confirmed until candle
    # index 3+right=5 has closed.
    assert row["swing_timestamp"] == df["timestamp"].iloc[3]
    assert row["confirmed_timestamp"] > df["timestamp"].iloc[3]
    assert row["confirmed_timestamp"] == df["timestamp"].iloc[5] + pd.Timedelta(minutes=1)

    # At the moment the swing candle itself closes, it must not yet be usable.
    swing_candle_close_time = df["timestamp"].iloc[3] + pd.Timedelta(minutes=1)
    usable_before_confirmation = swings[swings.confirmed_timestamp <= swing_candle_close_time]
    assert usable_before_confirmation.empty
