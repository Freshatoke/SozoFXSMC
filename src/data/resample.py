"""
Deterministic timeframe resampling (M1 -> M5, M1 -> M15, or any target offset).

OHLC aggregation rules:
    open   = first candle's open in the bucket
    high   = max high in the bucket
    low    = min low in the bucket
    close  = last candle's close in the bucket
    volume = sum of volume in the bucket (if present)

No look-ahead: a resampled bar is only "closed"/usable once its final
constituent M1 candle has closed. `resample_ohlc` labels bars by their bucket
start ("label='left'", "closed='left'") and additionally returns the bucket's
close timestamp so downstream code can enforce "don't use this bar until
bar_close_time has passed" if operating in a streaming/incremental context.
"""

from __future__ import annotations

import pandas as pd

AGG_MAP = {
    "open": "first",
    "high": "max",
    "low": "min",
    "close": "last",
}


def resample_ohlc(df: pd.DataFrame, target_timeframe: str, timestamp_col: str = "timestamp") -> pd.DataFrame:
    """Resample an M1 (or finer) OHLCV frame to a coarser timeframe.

    df must be sorted ascending by timestamp_col and contain no duplicate
    timestamps (use src.data.loader.load_m1_csv first).
    """
    if df.empty:
        cols = list(df.columns) + ["bar_close_time"]
        return pd.DataFrame(columns=cols)

    work = df.set_index(timestamp_col)
    agg = {k: v for k, v in AGG_MAP.items() if k in work.columns}
    if "volume" in work.columns:
        agg["volume"] = "sum"

    resampled = work.resample(target_timeframe, label="left", closed="left").agg(agg)
    resampled = resampled.dropna(subset=["open", "high", "low", "close"], how="all")

    resampled = resampled.reset_index().rename(columns={timestamp_col: timestamp_col})
    # bar_close_time = the timestamp at which this bar is fully formed/known,
    # i.e. the bucket end boundary. This is what strategy code must wait for.
    offset = pd.tseries.frequencies.to_offset(target_timeframe)
    resampled["bar_close_time"] = resampled[timestamp_col] + offset

    return resampled
