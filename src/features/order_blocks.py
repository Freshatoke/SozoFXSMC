"""
Order Block Engine.

Definitions (final, matching the Task 1 forward-reference):
    Bullish Order Block -- the final BEARISH candle immediately preceding
        qualifying BULLISH displacement.
    Bearish Order Block -- the final BULLISH candle immediately preceding
        qualifying BEARISH displacement.

An Order Block is only created when a qualifying displacement event (from
src.features.displacement) exists AND an opposite-direction candle can be
found within `config.lookback_candles` immediately before the displacement
started. If no such candle exists, no Order Block is created for that
displacement (recorded as a skip reason, not silently ignored -- see
`skipped` return value).

--------------------------------------------------------------------------
STATE MACHINE (documented per the "no object disappears without
explanation" requirement):

    ACTIVE
      -> created; zone [low, high] has not been touched by any candle since.
    PARTIALLY_MITIGATED
      -> a later candle's range overlaps the OB zone (wick or close inside
         it) but no candle has yet CLOSED all the way through the zone's
         far boundary.
    FULLY_MITIGATED
      -> a later candle CLOSES beyond the zone's far boundary (bullish OB:
         close < OB.low; bearish OB: close > OB.high). The zone has been
         "used up" -- terminal, unless invalidation criteria (below) apply.
    INVALIDATED
      -> a stricter terminal state than FULLY_MITIGATED: within
         `config.invalidation_lookahead` candles AFTER full mitigation, an
         opposing BOS/CHoCH structure event (see src.structure) confirms
         structure actually reversed against the OB's own direction. This
         distinguishes "the level got tapped and price moved on" (still
         FULLY_MITIGATED, structurally unconfirmed) from "the level failed
         and structure flipped" (INVALIDATED).
    ARCHIVED
      -> bookkeeping-only terminal state entered once
         `config.archive_after_candles` candles have elapsed since the
         object reached FULLY_MITIGATED or INVALIDATED. The row is never
         deleted; only `current_state` changes.

freshness_status: "FRESH" until first touch, then "MITIGATED".
mitigation_status: "UNMITIGATED" / "PARTIAL" / "FULL" mirrors current_state
    minus the ARCHIVED/INVALIDATED distinction, kept as its own field
    because strategies query mitigation independently of the
    archive/invalidation bookkeeping.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from config.settings import OrderBlockConfig, DEFAULT_ORDER_BLOCK_CONFIG
from src.features.displacement import detect_displacement, compute_candle_metrics


def _find_ob_candle(metrics: pd.DataFrame, displacement_start: int, direction: str, lookback: int) -> int | None:
    """Search backwards from displacement_start-1 for the last candle whose
    direction is opposite to the displacement direction."""
    opposite = "bearish" if direction == "bullish" else "bullish"
    lo = max(0, displacement_start - lookback)
    for idx in range(displacement_start - 1, lo - 1, -1):
        if metrics["direction"].iloc[idx] == opposite:
            return idx
    return None


def detect_order_blocks(
    df: pd.DataFrame,
    symbol: str,
    timeframe: str,
    config: OrderBlockConfig = DEFAULT_ORDER_BLOCK_CONFIG,
    structure_events: pd.DataFrame | None = None,
    timestamp_col: str = "timestamp",
    as_of_index: int | None = None,
) -> tuple[pd.DataFrame, list[dict]]:
    """Detect Order Blocks and compute their full lifecycle state as of the
    end of the series (or `as_of_index` if provided, useful for testing a
    partial history without look-ahead).

    Returns (order_blocks_df, skipped) where `skipped` documents every
    displacement event that did NOT produce an Order Block and why.
    """
    metrics = compute_candle_metrics(df, config.displacement)
    displacements = detect_displacement(df, config.displacement, timestamp_col=timestamp_col)

    n = len(metrics)
    last_index = (n - 1) if as_of_index is None else min(as_of_index, n - 1)

    ts = metrics[timestamp_col].reset_index(drop=True)
    high = metrics["high"].to_numpy()
    low = metrics["low"].to_numpy()
    close = metrics["close"].to_numpy()

    records = []
    skipped = []
    ob_seq = 0

    for _, disp in displacements.iterrows():
        if disp["end_index"] > last_index:
            continue  # displacement run hasn't fully completed yet as of this cutoff
        ob_idx = _find_ob_candle(metrics, disp["start_index"], disp["direction"], config.lookback_candles)
        if ob_idx is None:
            skipped.append({
                "displacement_id": disp["displacement_id"],
                "reason": f"no opposite-direction candle found within {config.lookback_candles} candles before displacement start",
            })
            continue

        ob_seq += 1
        direction = "bullish" if disp["direction"] == "bullish" else "bearish"
        ob_high = float(metrics["high"].iloc[ob_idx])
        ob_low = float(metrics["low"].iloc[ob_idx])
        ob_open = float(metrics["open"].iloc[ob_idx])
        ob_close = float(metrics["close"].iloc[ob_idx])
        ob_range = ob_high - ob_low
        body_size = abs(ob_close - ob_open)
        wick_ratio = ((ob_range - body_size) / ob_range) if ob_range > 0 else 0.0
        creation_timestamp = ts.iloc[ob_idx]

        # --- walk forward from the candle AFTER the displacement run completes
        # to compute lifecycle state. We deliberately do NOT start the
        # mitigation check at ob_idx+1: that candle is the displacement's own
        # first candle, which (by construction of continuous OHLC data) opens
        # exactly at the OB's boundary and would trivially register as a
        # "touch" on every OB. Mitigation should describe the RETURN move
        # after the impulsive leg finished, not the impulsive leg itself. ---
        walk_start = int(disp["end_index"]) + 1
        walk_end = last_index
        first_touch_timestamp = None
        full_mitigation_timestamp = None
        full_mitigation_index = None
        current_state = "ACTIVE"
        freshness_status = "FRESH"
        mitigation_status = "UNMITIGATED"

        if walk_start <= walk_end:
            w_high = high[walk_start:walk_end + 1]
            w_low = low[walk_start:walk_end + 1]
            w_close = close[walk_start:walk_end + 1]

            if direction == "bullish":
                touched_mask = w_low <= ob_high
                closed_through_mask = w_close < ob_low
            else:
                touched_mask = w_high >= ob_low
                closed_through_mask = w_close > ob_high

            touched_idx = np.argmax(touched_mask) if touched_mask.any() else None
            closed_idx = np.argmax(closed_through_mask) if closed_through_mask.any() else None

            if touched_idx is not None:
                first_touch_timestamp = ts.iloc[walk_start + touched_idx]
                freshness_status = "MITIGATED"
                current_state = "PARTIALLY_MITIGATED"
                mitigation_status = "PARTIAL"

            if closed_idx is not None:
                full_mitigation_index = walk_start + closed_idx
                full_mitigation_timestamp = ts.iloc[full_mitigation_index]
                current_state = "FULLY_MITIGATED"
                mitigation_status = "FULL"
                if touched_idx is None:
                    freshness_status = "MITIGATED"

                # invalidation check: opposing structure event within lookahead
                if structure_events is not None and not structure_events.empty:
                    opposing_direction = "bearish" if direction == "bullish" else "bullish"
                    lookahead_end_ts = ts.iloc[min(full_mitigation_index + config.invalidation_lookahead, last_index)]
                    mask = (
                        (structure_events["direction"] == opposing_direction)
                        & (structure_events["break_candle_timestamp"] > full_mitigation_timestamp)
                        & (structure_events["break_candle_timestamp"] <= lookahead_end_ts)
                    )
                    if mask.any():
                        current_state = "INVALIDATED"

                # archive check
                terminal_index = full_mitigation_index
                if (last_index - terminal_index) >= config.archive_after_candles:
                    current_state = "ARCHIVED"

        displacement_reference = {
            "displacement_id": disp["displacement_id"],
            "start_timestamp": disp["start_timestamp"],
            "end_timestamp": disp["end_timestamp"],
            "total_range": disp["total_range"],
            "num_candles": disp["num_candles"],
        }

        # --- quality score (deterministic, rule-based; see docs) ---
        freshness_score = 1.0 if current_state == "ACTIVE" else (0.5 if current_state == "PARTIALLY_MITIGATED" else 0.0)
        atr_at_creation = float(metrics["atr"].iloc[ob_idx]) if pd.notna(metrics["atr"].iloc[ob_idx]) else 0.0
        displacement_score = min(disp["total_range"] / (atr_at_creation * 3), 1.0) if atr_at_creation > 0 else 0.0
        body_score = 1.0 - wick_ratio
        age_candles = last_index - ob_idx
        age_score = max(0.0, 1.0 - age_candles / max(config.archive_after_candles, 1))
        quality_score = round(
            0.30 * freshness_score + 0.30 * displacement_score + 0.20 * body_score + 0.20 * age_score, 4
        )

        records.append({
            "ob_id": f"{symbol}_{timeframe}_OB_{ob_seq}",
            "direction": direction,
            "timeframe": timeframe,
            "creation_timestamp": creation_timestamp,
            "creation_index": ob_idx,
            "high": ob_high,
            "low": ob_low,
            "open": ob_open,
            "close": ob_close,
            "body_size": body_size,
            "wick_ratio": wick_ratio,
            "displacement_reference": displacement_reference,
            "freshness_status": freshness_status,
            "mitigation_status": mitigation_status,
            "first_touch_timestamp": first_touch_timestamp,
            "full_mitigation_timestamp": full_mitigation_timestamp,
            "current_state": current_state,
            "quality_score": quality_score,
        })

    columns = [
        "ob_id", "direction", "timeframe", "creation_timestamp", "creation_index",
        "high", "low", "open", "close", "body_size", "wick_ratio",
        "displacement_reference", "freshness_status", "mitigation_status",
        "first_touch_timestamp", "full_mitigation_timestamp", "current_state", "quality_score",
    ]
    return pd.DataFrame.from_records(records, columns=columns), skipped
