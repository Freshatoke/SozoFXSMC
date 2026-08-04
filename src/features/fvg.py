"""
Fair Value Gap (FVG) Engine.

Definition (classic 3-candle imbalance):
    Bullish FVG at index i (the middle/impulse candle): high[i-1] < low[i+1].
        zone = [high[i-1], low[i+1]]  (bottom, top)
    Bearish FVG at index i: low[i-1] > high[i+1].
        zone = [high[i+1], low[i-1]]  (bottom, top)

The gap cannot be confirmed until candle i+1 has fully CLOSED (its low/high
are otherwise not final), so `creation_timestamp` is the close time of
candle i+1, not its open time -- this keeps the engine look-ahead free in a
streaming context even though this implementation processes a completed
historical DataFrame in one pass.

Mitigation model:
    filled_percentage -- how much of the [bottom, top] zone has been
        traded back into by subsequent candles, 0-100.
    Consequent Encroachment (CE) -- the 50% level of the zone; we record
        whether price has reached it (`ce_reached`).
    PARTIALLY_FILLED  -- 0 < filled_percentage < 100
    FULLY_MITIGATED   -- filled_percentage >= 100 (a subsequent candle's
        range has fully covered the zone from the counter-trend side)
    EXPIRED           -- `config.expire_after_candles` have elapsed with no
        interaction at all (filled_percentage == 0) -- bookkeeping only,
        the row is retained.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from config.settings import FVGConfig, DEFAULT_FVG_CONFIG


def _infer_interval(ts: pd.Series) -> pd.Timedelta:
    diffs = ts.diff().dropna()
    return diffs.mode().iloc[0] if not diffs.empty else pd.Timedelta(0)


def detect_fvgs(
    df: pd.DataFrame,
    symbol: str,
    timeframe: str,
    config: FVGConfig = DEFAULT_FVG_CONFIG,
    timestamp_col: str = "timestamp",
    as_of_index: int | None = None,
) -> pd.DataFrame:
    n = len(df)
    last_index = (n - 1) if as_of_index is None else min(as_of_index, n - 1)

    ts = df[timestamp_col].reset_index(drop=True)
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    interval = _infer_interval(ts)

    records = []
    fvg_seq = 0

    for i in range(1, n - 1):
        if i + 1 > last_index:
            break  # the confirming candle hasn't closed yet as of this cutoff

        bullish_gap = high[i - 1] < low[i + 1]
        bearish_gap = low[i - 1] > high[i + 1]

        if not bullish_gap and not bearish_gap:
            continue

        direction = "bullish" if bullish_gap else "bearish"
        bottom = float(high[i - 1]) if bullish_gap else float(high[i + 1])
        top = float(low[i + 1]) if bullish_gap else float(low[i - 1])
        size = top - bottom
        if size <= config.min_gap_size:
            continue

        fvg_seq += 1
        creation_timestamp = ts.iloc[i + 1] + interval
        creation_index = i + 1

        walk_start = creation_index + 1
        filled_percentage = 0.0
        ce_reached = False
        active_status = "ACTIVE"

        if walk_start <= last_index:
            w_low = low[walk_start:last_index + 1]
            w_high = high[walk_start:last_index + 1]
            ce_level = bottom + size / 2

            if direction == "bullish":
                # price trading back down into the zone from above
                deepest_penetration = top - w_low.min() if len(w_low) else 0.0
                deepest_penetration = max(0.0, min(deepest_penetration, size))
                ce_reached = bool((w_low <= ce_level).any())
            else:
                deepest_penetration = w_high.max() - bottom if len(w_high) else 0.0
                deepest_penetration = max(0.0, min(deepest_penetration, size))
                ce_reached = bool((w_high >= ce_level).any())

            filled_percentage = round(100.0 * deepest_penetration / size, 2) if size > 0 else 0.0

            age_candles = last_index - creation_index
            if filled_percentage >= 100.0:
                active_status = "FULLY_MITIGATED"
            elif filled_percentage > 0.0:
                active_status = "PARTIALLY_FILLED"
            elif age_candles >= config.expire_after_candles:
                active_status = "EXPIRED"
            else:
                active_status = "ACTIVE"

        age = last_index - creation_index

        records.append({
            "fvg_id": f"{symbol}_{timeframe}_FVG_{fvg_seq}",
            "direction": direction,
            "timeframe": timeframe,
            "top": top,
            "bottom": bottom,
            "size": size,
            "consequent_encroachment": bottom + size / 2,
            "ce_reached": ce_reached,
            "creation_timestamp": creation_timestamp,
            "creation_index": creation_index,
            "impulse_candle_index": i,
            "filled_percentage": filled_percentage,
            "mitigation_state": active_status,
            "age": age,
            "active_status": active_status,
        })

    columns = [
        "fvg_id", "direction", "timeframe", "top", "bottom", "size",
        "consequent_encroachment", "ce_reached", "creation_timestamp",
        "creation_index", "impulse_candle_index", "filled_percentage",
        "mitigation_state", "age", "active_status",
    ]
    return pd.DataFrame.from_records(records, columns=columns)
