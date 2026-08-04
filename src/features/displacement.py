"""
Displacement Engine.

Displacement = an unusually strong, directional price move used elsewhere
(Order Block engine) as the trigger that qualifies an Order Block. This
module is standalone and reusable: it only measures and explains
displacement, it does not know about Order Blocks.

A single candle qualifies as an "impulsive" candle when at least
`min_conditions_met` of the following hold:
    1. body_size >= atr_multiplier * ATR(atr_period)
    2. body_size >= body_multiple * average body size over recent_body_lookback candles
    3. body_ratio (body_size / candle_range) >= min_body_ratio

Consecutive impulsive candles in the same direction are grouped into a
single "displacement event" (a run). Each event records enough detail to
explain *why* it qualified (which conditions were met, and their values).

No look-ahead: every metric for candle i (ATR, average recent body) only
uses candles up to and including i.
"""

from __future__ import annotations

import pandas as pd
import numpy as np

from config.settings import DisplacementConfig, DEFAULT_DISPLACEMENT_CONFIG


def _true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr


def compute_candle_metrics(df: pd.DataFrame, config: DisplacementConfig = DEFAULT_DISPLACEMENT_CONFIG) -> pd.DataFrame:
    """Return df with added columns: body_size, range, body_ratio, direction,
    atr, avg_recent_body -- all computed causally (no look-ahead)."""
    out = df.copy()
    out["body_size"] = (out["close"] - out["open"]).abs()
    out["range"] = (out["high"] - out["low"]).replace(0, np.nan)
    out["body_ratio"] = (out["body_size"] / out["range"]).fillna(0.0)
    out["direction"] = np.where(out["close"] > out["open"], "bullish",
                          np.where(out["close"] < out["open"], "bearish", "flat"))

    tr = _true_range(out)
    out["atr"] = tr.rolling(config.atr_period, min_periods=1).mean()
    out["avg_recent_body"] = out["body_size"].rolling(config.recent_body_lookback, min_periods=1).mean().shift(1)
    return out


def detect_displacement(
    df: pd.DataFrame,
    config: DisplacementConfig = DEFAULT_DISPLACEMENT_CONFIG,
    timestamp_col: str = "timestamp",
) -> pd.DataFrame:
    """Detect qualifying displacement events (runs of impulsive same-direction candles).

    Returns a DataFrame, one row per displacement event, with columns:
        displacement_id, direction, start_index, end_index,
        start_timestamp, end_timestamp, num_candles, total_range,
        avg_body_ratio, conditions (per-candle explanation list)
    """
    metrics = compute_candle_metrics(df, config)
    n = len(metrics)

    qualifies = np.zeros(n, dtype=bool)
    reasons = [None] * n

    for i in range(n):
        row = metrics.iloc[i]
        if row["direction"] == "flat":
            continue
        cond1 = bool(row["atr"] > 0 and row["body_size"] >= config.atr_multiplier * row["atr"])
        avg_body = row["avg_recent_body"]
        cond2 = bool(pd.notna(avg_body) and avg_body > 0 and row["body_size"] >= config.body_multiple * avg_body)
        cond3 = bool(row["body_ratio"] >= config.min_body_ratio)
        conditions_met = sum([cond1, cond2, cond3])
        reasons[i] = {
            "atr_condition": cond1,
            "avg_body_condition": cond2,
            "body_ratio_condition": cond3,
            "conditions_met": conditions_met,
            "body_size": float(row["body_size"]),
            "atr": float(row["atr"]) if pd.notna(row["atr"]) else None,
            "avg_recent_body": float(avg_body) if pd.notna(avg_body) else None,
            "body_ratio": float(row["body_ratio"]),
        }
        qualifies[i] = conditions_met >= config.min_conditions_met

    events = []
    i = 0
    event_seq = 0
    ts = metrics[timestamp_col].reset_index(drop=True)
    direction_arr = metrics["direction"].to_numpy()

    while i < n:
        if not qualifies[i]:
            i += 1
            continue
        start = i
        direction = direction_arr[i]
        j = i
        while j + 1 < n and qualifies[j + 1] and direction_arr[j + 1] == direction:
            j += 1
        event_seq += 1
        total_range = float(metrics["high"].iloc[start:j + 1].max() - metrics["low"].iloc[start:j + 1].min())
        avg_body_ratio = float(metrics["body_ratio"].iloc[start:j + 1].mean())
        events.append({
            "displacement_id": f"disp_{event_seq}",
            "direction": direction,
            "start_index": start,
            "end_index": j,
            "start_timestamp": ts.iloc[start],
            "end_timestamp": ts.iloc[j],
            "num_candles": j - start + 1,
            "total_range": total_range,
            "avg_body_ratio": avg_body_ratio,
            "reasons": [reasons[k] for k in range(start, j + 1)],
        })
        i = j + 1

    columns = [
        "displacement_id", "direction", "start_index", "end_index",
        "start_timestamp", "end_timestamp", "num_candles", "total_range",
        "avg_body_ratio", "reasons",
    ]
    return pd.DataFrame.from_records(events, columns=columns)
