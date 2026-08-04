"""
Engulfing Candle Engine.

Definitions:
    Bullish Engulfing at index i+1: candle i is bearish, candle i+1 is
        bullish, and candle i+1's real body fully contains candle i's real
        body (open[i+1] <= close[i] and close[i+1] >= open[i]).
    Bearish Engulfing at index i+1: candle i is bullish, candle i+1 is
        bearish, and candle i+1's real body fully contains candle i's real
        body (open[i+1] >= close[i] and close[i+1] <= open[i]).

Strength classification is a deterministic ratio of the engulfing candle's
body size to the engulfed candle's body size:
    body_ratio >= config.strong_body_ratio  -> STRONG
    body_ratio >= config.normal_body_ratio  -> NORMAL
    otherwise                               -> WEAK

`displacement_backed` reuses the Displacement Engine's per-candle
qualification (no duplicated logic) to flag whether the engulfing candle
itself also qualifies as a displacement candle.
"""

from __future__ import annotations

import pandas as pd

from config.settings import EngulfingConfig, DEFAULT_ENGULFING_CONFIG, DEFAULT_DISPLACEMENT_CONFIG
from src.features.displacement import compute_candle_metrics


def detect_engulfing(
    df: pd.DataFrame,
    config: EngulfingConfig = DEFAULT_ENGULFING_CONFIG,
    displacement_config=DEFAULT_DISPLACEMENT_CONFIG,
    timestamp_col: str = "timestamp",
) -> pd.DataFrame:
    metrics = compute_candle_metrics(df, displacement_config)
    n = len(metrics)
    ts = metrics[timestamp_col].reset_index(drop=True)
    open_ = metrics["open"].to_numpy()
    close = metrics["close"].to_numpy()
    body_size = metrics["body_size"].to_numpy()
    atr = metrics["atr"].to_numpy()
    avg_recent_body = metrics["avg_recent_body"].to_numpy()

    records = []
    seq = 0

    for i in range(n - 1):
        prev_open, prev_close = open_[i], close[i]
        cur_open, cur_close = open_[i + 1], close[i + 1]
        prev_bearish = prev_close < prev_open
        prev_bullish = prev_close > prev_open
        cur_bullish = cur_close > cur_open
        cur_bearish = cur_close < cur_open

        direction = None
        if prev_bearish and cur_bullish and cur_open <= prev_close and cur_close >= prev_open:
            direction = "bullish"
        elif prev_bullish and cur_bearish and cur_open >= prev_close and cur_close <= prev_open:
            direction = "bearish"

        if direction is None:
            continue

        seq += 1
        engulfed_body = body_size[i]
        engulfing_body = body_size[i + 1]
        body_ratio = (engulfing_body / engulfed_body) if engulfed_body > 0 else float("inf")

        if body_ratio >= config.strong_body_ratio:
            strength = "STRONG"
        elif body_ratio >= config.normal_body_ratio:
            strength = "NORMAL"
        else:
            strength = "WEAK"

        cond1 = bool(atr[i + 1] > 0 and engulfing_body >= displacement_config.atr_multiplier * atr[i + 1])
        cond2 = bool(pd.notna(avg_recent_body[i + 1]) and avg_recent_body[i + 1] > 0
                     and engulfing_body >= displacement_config.body_multiple * avg_recent_body[i + 1])
        displacement_backed = bool(cond1 or cond2)
        if displacement_backed:
            strength = "STRONG" if strength != "WEAK" else strength

        quality_score = round(min(body_ratio / (config.strong_body_ratio * 1.5), 1.0) * 0.7
                               + (0.3 if displacement_backed else 0.0), 4)

        records.append({
            "engulfing_id": f"ENGULF_{seq}",
            "direction": direction,
            "strength": strength,
            "timestamp": ts.iloc[i + 1],
            "candle_index": i + 1,
            "engulfed_candle_index": i,
            "body_ratio": round(float(body_ratio), 4) if body_ratio != float("inf") else None,
            "displacement_backed": displacement_backed,
            "quality_score": quality_score,
        })

    columns = [
        "engulfing_id", "direction", "strength", "timestamp", "candle_index",
        "engulfed_candle_index", "body_ratio", "displacement_backed", "quality_score",
    ]
    return pd.DataFrame.from_records(records, columns=columns)
