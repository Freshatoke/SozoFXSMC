"""
Confluence Engine.

Read-only context aggregator. It makes NO trading decisions -- it answers
independent, queryable questions about the current state of every other
feature engine as of a given point in time ("as_of_index"), e.g.:

    "Is there an active bullish Order Block right now?"
    "Was PDL swept?"
    "Is the current structure state bullish, and was the last event a CHoCH?"

Each question is exposed as its own small function so callers (future
strategies) can query exactly what they need instead of depending on a
monolithic blob. `build_confluence_snapshot` simply composes all of them
into one row for convenience / dataset generation.

Look-ahead safety: every sub-engine call below is given the SAME
`as_of_index` cutoff (or an equivalent truncated slice of the candle
history), so nothing here can see beyond "now".

PERFORMANCE NOTE: each snapshot recomputes every feature engine from the
start of history up to `as_of_index`, which is O(n) per snapshot. Calling
this once per candle over millions of rows is therefore O(n^2) and NOT
recommended -- use it at specific decision points (e.g. once per new
candle close in a backtest loop, or at sparse intervals for research). A
future task should replace the FVG/OB/liquidity recomputation with
incremental/interval-indexed state if a dense per-candle confluence
timeline is needed.
"""

from __future__ import annotations

import pandas as pd

from config.settings import (
    DEFAULT_STRUCTURE_CONFIG, DEFAULT_ORDER_BLOCK_CONFIG, DEFAULT_FVG_CONFIG,
    DEFAULT_LIQUIDITY_CONFIG, SwingConfig,
)
from src.structure.swings import detect_swings
from src.structure.market_structure import detect_structure_events
from src.features.order_blocks import detect_order_blocks
from src.features.fvg import detect_fvgs
from src.features.liquidity import detect_liquidity_levels
from src.features.reference_levels import compute_reference_levels, compute_weekend_gaps
from src.features.engulfing import detect_engulfing
from src.features.sessions import compute_sessions


def _level_swept(df: pd.DataFrame, level_price: float, available_from, as_of_ts, direction: str) -> bool:
    """direction='above' -> swept if a candle's high > level and close < level (buy-side sweep);
    direction='below' -> swept if a candle's low < level and close > level (sell-side sweep)."""
    window = df[(df["timestamp"] > available_from) & (df["timestamp"] <= as_of_ts)]
    if window.empty:
        return False
    if direction == "above":
        return bool(((window["high"] > level_price) & (window["close"] < level_price)).any())
    return bool(((window["low"] < level_price) & (window["close"] > level_price)).any())


def build_confluence_snapshot(
    df: pd.DataFrame,
    as_of_index: int,
    symbol: str,
    timeframe: str,
    structure_config=DEFAULT_STRUCTURE_CONFIG,
    ob_config=DEFAULT_ORDER_BLOCK_CONFIG,
    fvg_config=DEFAULT_FVG_CONFIG,
    liquidity_config=DEFAULT_LIQUIDITY_CONFIG,
    recent_engulfing_window: int = 3,
) -> dict:
    as_of_index = min(as_of_index, len(df) - 1)
    as_of_ts = df["timestamp"].iloc[as_of_index]
    history = df.iloc[: as_of_index + 1].reset_index(drop=True)

    swings = detect_swings(history, config=structure_config.swing, timeframe_label=timeframe)
    structure_events = detect_structure_events(history, swings, symbol=symbol, timeframe=timeframe, config=structure_config)

    if not structure_events.empty:
        last_event = structure_events.iloc[-1]
        structure_state = last_event["new_structure_state"]
        last_event_type = last_event["event_type"]
        last_event_direction = last_event["direction"]
    else:
        structure_state, last_event_type, last_event_direction = "UNKNOWN", None, None

    order_blocks, _ = detect_order_blocks(
        history, symbol, timeframe, config=ob_config, structure_events=structure_events, as_of_index=as_of_index,
    )
    active_ob = order_blocks[order_blocks.current_state.isin(["ACTIVE", "PARTIALLY_MITIGATED"])] if not order_blocks.empty else order_blocks
    fresh_ob = order_blocks[order_blocks.freshness_status == "FRESH"] if not order_blocks.empty else order_blocks

    fvgs = detect_fvgs(history, symbol, timeframe, config=fvg_config, as_of_index=as_of_index)
    active_fvg = fvgs[fvgs.active_status.isin(["ACTIVE", "PARTIALLY_FILLED"])] if not fvgs.empty else fvgs

    liquidity = detect_liquidity_levels(history, symbol, timeframe, config=liquidity_config, as_of_index=as_of_index)

    reference_levels = compute_reference_levels(history)
    weekend_gaps = compute_weekend_gaps(history, as_of_index=as_of_index)
    engulfing = detect_engulfing(history)
    sessions = compute_sessions(history)

    pdh_swept = pdl_swept = False
    ref_now = reference_levels[reference_levels.available_from <= as_of_ts] if not reference_levels.empty else reference_levels
    if not ref_now.empty:
        pdh_rows = ref_now[ref_now.level_type == "PDH"]
        pdl_rows = ref_now[ref_now.level_type == "PDL"]
        if not pdh_rows.empty:
            r = pdh_rows.iloc[-1]
            pdh_swept = _level_swept(history, r["value"], r["available_from"], as_of_ts, "above")
        if not pdl_rows.empty:
            r = pdl_rows.iloc[-1]
            pdl_swept = _level_swept(history, r["value"], r["available_from"], as_of_ts, "below")

    asian_low_swept = asian_high_swept = False
    tokyo_sessions = sessions[sessions.session_name == "tokyo"] if not sessions.empty else sessions
    if not tokyo_sessions.empty:
        last_asia = tokyo_sessions[tokyo_sessions.end_utc <= as_of_ts]
        if not last_asia.empty:
            r = last_asia.iloc[-1]
            asian_low_swept = _level_swept(history, r["low"], r["end_utc"], as_of_ts, "below")
            asian_high_swept = _level_swept(history, r["high"], r["end_utc"], as_of_ts, "above")

    strong_engulfing_recent = False
    if not engulfing.empty:
        recent = engulfing[engulfing.candle_index >= max(0, as_of_index - recent_engulfing_window)]
        strong_engulfing_recent = bool((recent.strength == "STRONG").any())

    open_weekend_gap = bool((weekend_gaps.state != "FILLED").any()) if not weekend_gaps.empty else False

    return {
        "timestamp": as_of_ts,
        "symbol": symbol,
        "timeframe": timeframe,
        "structure_state": structure_state,
        "last_structure_event_type": last_event_type,
        "last_structure_event_direction": last_event_direction,
        "active_bullish_ob_count": int((active_ob.direction == "bullish").sum()) if not active_ob.empty else 0,
        "active_bearish_ob_count": int((active_ob.direction == "bearish").sum()) if not active_ob.empty else 0,
        "fresh_bullish_ob": bool(((fresh_ob.direction == "bullish").any())) if not fresh_ob.empty else False,
        "fresh_bearish_ob": bool(((fresh_ob.direction == "bearish").any())) if not fresh_ob.empty else False,
        "active_bullish_fvg_count": int((active_fvg.direction == "bullish").sum()) if not active_fvg.empty else 0,
        "active_bearish_fvg_count": int((active_fvg.direction == "bearish").sum()) if not active_fvg.empty else 0,
        "pdh_swept": pdh_swept,
        "pdl_swept": pdl_swept,
        "asian_low_swept": asian_low_swept,
        "asian_high_swept": asian_high_swept,
        "strong_engulfing_recent": strong_engulfing_recent,
        "open_weekend_gap": open_weekend_gap,
        "num_liquidity_levels_active": int((liquidity.state == "ACTIVE").sum()) if not liquidity.empty else 0,
    }


def generate_confluence_dataset(df: pd.DataFrame, symbol: str, timeframe: str, stride: int = 50, **kwargs) -> pd.DataFrame:
    """Convenience batch generator: builds a confluence snapshot every
    `stride` candles. See the performance note in the module docstring
    before reducing `stride` on large datasets."""
    rows = []
    for i in range(0, len(df), stride):
        rows.append(build_confluence_snapshot(df, i, symbol, timeframe, **kwargs))
    return pd.DataFrame(rows)
