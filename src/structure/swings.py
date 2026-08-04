"""
Deterministic, look-ahead-free swing-high / swing-low detection.

Definition (method="fractal", the only method implemented in v0.1):
    Candle i is a CONFIRMED SWING HIGH if:
        high[i] > high[i-left ... i-1]   (strictly greater than each of the `left` prior highs)
        high[i] > high[i+1 ... i+right]  (strictly greater than each of the `right` following highs)
    Candle i is a CONFIRMED SWING LOW analogously with `low` and strict "<".

Critically, a swing at candle i cannot be known until candle i+right has
CLOSED. We therefore always emit two separate timestamps:

    swing_timestamp        -- the timestamp of the candle that IS the swing (when it occurred)
    confirmed_timestamp    -- the close time of candle i+right (when the swing became knowable)

Any consumer (BOS/CHoCH engine, strategies) must filter
`df[df.confirmed_timestamp <= current_time]` before using a swing. Using
swing_timestamp for that filter would leak future information.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from config.settings import SwingConfig, DEFAULT_SWING_CONFIG


def _infer_interval(timestamps: pd.Series) -> pd.Timedelta:
    diffs = timestamps.diff().dropna()
    if diffs.empty:
        return pd.Timedelta(0)
    return diffs.mode().iloc[0]


def detect_swings(
    df: pd.DataFrame,
    config: SwingConfig = DEFAULT_SWING_CONFIG,
    timestamp_col: str = "timestamp",
    timeframe_label: str | None = None,
) -> pd.DataFrame:
    """Detect confirmed swing highs/lows on a single-timeframe OHLC frame.

    df must be sorted ascending by timestamp_col with a reset integer index.

    Returns a DataFrame with columns:
        swing_timestamp, confirmed_timestamp, price, swing_type,
        timeframe, left, right, method, candle_index
    sorted by confirmed_timestamp ascending (the order in which they become
    usable to a strategy).
    """
    if config.method != "fractal":
        raise NotImplementedError(f"Swing method '{config.method}' is not implemented in v0.1")

    left, right = config.left, config.right
    n = len(df)
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    ts = df[timestamp_col].reset_index(drop=True)
    interval = _infer_interval(ts)
    # Task 7.4 PERFORMANCE NOTE: ts.iloc[i] (pandas positional access) was
    # called up to 2x per candle in this loop, which profiling showed as a
    # measurable contributor across the full structure-detection pipeline.
    # ts_list is a plain Python list built once; ts_list[i] yields the exact
    # same pd.Timestamp objects as ts.iloc[i] would, without per-call pandas
    # indexing overhead.
    ts_list = ts.tolist()

    records = []
    # Only interior candles that have `left` candles before and `right` after
    # can ever be evaluated.
    for i in range(left, n - right):
        window_high_left = highs[i - left:i]
        window_high_right = highs[i + 1:i + 1 + right]
        window_low_left = lows[i - left:i]
        window_low_right = lows[i + 1:i + 1 + right]

        confirm_candle_ts = ts_list[i + right]
        confirmed_timestamp = confirm_candle_ts + interval  # close time of the confirming candle

        if highs[i] > window_high_left.max() and highs[i] > window_high_right.max():
            records.append({
                "swing_timestamp": ts_list[i],
                "confirmed_timestamp": confirmed_timestamp,
                "price": float(highs[i]),
                "swing_type": "high",
                "timeframe": timeframe_label,
                "left": left,
                "right": right,
                "method": config.method,
                "candle_index": i,
            })

        if lows[i] < window_low_left.min() and lows[i] < window_low_right.min():
            records.append({
                "swing_timestamp": ts_list[i],
                "confirmed_timestamp": confirmed_timestamp,
                "price": float(lows[i]),
                "swing_type": "low",
                "timeframe": timeframe_label,
                "left": left,
                "right": right,
                "method": config.method,
                "candle_index": i,
            })

    out = pd.DataFrame.from_records(
        records,
        columns=[
            "swing_timestamp", "confirmed_timestamp", "price", "swing_type",
            "timeframe", "left", "right", "method", "candle_index",
        ],
    )
    if not out.empty:
        out = out.sort_values(["confirmed_timestamp", "candle_index"]).reset_index(drop=True)
    return out
