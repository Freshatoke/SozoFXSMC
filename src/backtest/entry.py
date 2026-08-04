"""
Entry Engine: determines exactly when (if ever) a signal becomes an
executed trade.

Each method is a plain function `(signal, m1, config) -> Optional[dict]`
registered in `ENTRY_METHODS` by name, so a new method can be added later
without touching any existing one -- just register it.

Returned dict: `{"entry_price": float, "entry_timestamp": pd.Timestamp}`,
or `None` if the entry condition never triggered within
`config.max_wait_candles` M1 candles after the signal (the trade is then
marked EXPIRED by the engine).

These functions return the RAW theoretical trigger price -- spread,
slippage, and commission are applied afterward by `src.backtest.execution`,
never mixed in here, so each concern stays independently testable.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from config.settings import EntryConfig


def _zone_edges(signal) -> tuple:
    """(proximal, distal): proximal = the edge of the OB zone closer to
    price at signal time (hit first on a pullback), distal = the far edge
    (a deeper retracement). Bullish OBs sit below price (proximal=high,
    distal=low); bearish OBs sit above price (proximal=low, distal=high)."""
    low, high = signal.entry_zone
    return (high, low) if signal.direction == "bullish" else (low, high)


def _window_after(signal, m1: pd.DataFrame, max_wait_candles: int) -> pd.DataFrame:
    # Task 7.4 PERFORMANCE NOTE: m1[m1["timestamp"] > signal.timestamp] was
    # a full boolean filter over the ENTIRE m1 frame, run once per signal --
    # for a multi-year dataset with thousands of signals this is O(n *
    # num_signals), the single largest remaining bottleneck found when the
    # full 6.5-year campaign (unlike the smaller sizes used during earlier
    # profiling passes) ran for hours instead of tens of minutes. m1 is
    # sorted ascending by timestamp by construction, so searchsorted finds
    # the same cutoff position in O(log n); the result is only ever sliced
    # down to `max_wait_candles` afterward anyway, so `.iloc[start:start +
    # max_wait_candles]` is exactly equivalent to the original filter+head
    # but never touches rows beyond what's actually needed.
    start = m1["timestamp"].searchsorted(signal.timestamp, side="right")
    return m1.iloc[start:start + max_wait_candles]


def entry_market(signal, m1: pd.DataFrame, config: EntryConfig) -> Optional[dict]:
    """Enter at the open of the next candle after the signal -- the
    simplest, most conservative "just take it" entry."""
    window = _window_after(signal, m1, config.max_wait_candles)
    if window.empty:
        return None
    row = window.iloc[0]
    return {"entry_price": float(row["open"]), "entry_timestamp": row["timestamp"]}


def entry_confirmation_close(signal, m1: pd.DataFrame, config: EntryConfig) -> Optional[dict]:
    """Enter at the signal's own triggering candle's close -- the
    confirmation (CHoCH/sweep/etc.) that produced the signal already
    closed at `signal.timestamp`, so this is "trade the confirmation
    candle's close price directly"."""
    # Task 7.4 PERFORMANCE NOTE: same O(n)-per-signal issue as
    # _window_after above -- m1 is sorted ascending by timestamp, so an
    # exact-match lookup is a searchsorted position check, not a full
    # equality filter over the whole frame.
    idx = m1["timestamp"].searchsorted(signal.timestamp, side="left")
    if idx >= len(m1) or m1["timestamp"].iloc[idx] != signal.timestamp:
        return None
    return {"entry_price": float(m1["close"].iloc[idx]), "entry_timestamp": signal.timestamp}


def _entry_on_zone_touch(signal, m1: pd.DataFrame, config: EntryConfig, price_level: float) -> Optional[dict]:
    window = _window_after(signal, m1, config.max_wait_candles)
    if window.empty:
        return None
    touched = window[(window["low"] <= price_level) & (window["high"] >= price_level)]
    if touched.empty:
        return None
    row = touched.iloc[0]
    return {"entry_price": float(price_level), "entry_timestamp": row["timestamp"]}


def entry_ob_touch(signal, m1: pd.DataFrame, config: EntryConfig) -> Optional[dict]:
    proximal, _ = _zone_edges(signal)
    return _entry_on_zone_touch(signal, m1, config, proximal)


def entry_ob_proximal_edge(signal, m1: pd.DataFrame, config: EntryConfig) -> Optional[dict]:
    proximal, _ = _zone_edges(signal)
    return _entry_on_zone_touch(signal, m1, config, proximal)


def entry_ob_distal_edge(signal, m1: pd.DataFrame, config: EntryConfig) -> Optional[dict]:
    _, distal = _zone_edges(signal)
    return _entry_on_zone_touch(signal, m1, config, distal)


def entry_ob_midpoint(signal, m1: pd.DataFrame, config: EntryConfig) -> Optional[dict]:
    low, high = signal.entry_zone
    midpoint = (low + high) / 2
    return _entry_on_zone_touch(signal, m1, config, midpoint)


ENTRY_METHODS = {
    "market": entry_market,
    "confirmation_close": entry_confirmation_close,
    "ob_touch": entry_ob_touch,
    "ob_midpoint": entry_ob_midpoint,
    "ob_proximal_edge": entry_ob_proximal_edge,
    "ob_distal_edge": entry_ob_distal_edge,
}


def apply_entry_buffer(entry_price: float, direction: str, config: EntryConfig, pip_size: float) -> float:
    """Shifts the entry price slightly against the trader (a conservative
    buffer) by `entry_buffer_pips`, e.g. to model waiting for a small extra
    confirmation move before actually filling."""
    buffer = config.entry_buffer_pips * pip_size
    return entry_price + buffer if direction == "bullish" else entry_price - buffer


def resolve_entry(signal, m1: pd.DataFrame, config: EntryConfig, pip_size: float) -> Optional[dict]:
    method = ENTRY_METHODS.get(config.method)
    if method is None:
        raise ValueError(f"Unknown entry method: {config.method}")
    result = method(signal, m1, config)
    if result is None:
        return None
    result = dict(result)
    result["entry_price"] = apply_entry_buffer(result["entry_price"], signal.direction, config, pip_size)
    return result
