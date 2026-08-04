"""
Stop-Loss Engine: configurable initial stop placement, one function per
method registered in `STOP_LOSS_METHODS` (add new methods by registering,
never by editing an existing one). Every stop is computed ONCE at entry
time using only information available up to `entry_timestamp` -- no
method here ever looks forward.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from config.settings import StopLossConfig


def _true_range_atr(m1: pd.DataFrame, entry_timestamp, period: int) -> float:
    # Task 7.4 PERFORMANCE NOTE: m1[m1["timestamp"] <= entry_timestamp] was
    # a full boolean filter over the ENTIRE m1 frame (up to ~2M rows for a
    # multi-year dataset), executed once per trade just to reach the last
    # `period + 1` rows before entry_timestamp -- an O(n) scan per trade,
    # i.e. O(n * num_trades) overall. m1 is sorted ascending by timestamp
    # by construction, so Series.searchsorted (O(log n), backed by numpy
    # binary search) finds the same cutoff position that the boolean
    # filter's row count would have, and a positional iloc slice for the
    # last `period + 1` rows before that cutoff is identical to
    # `m1[m1["timestamp"] <= entry_timestamp].tail(period + 1)` (verified:
    # both select exactly the same maximal-index candles-before-or-at
    # entry_timestamp).
    end_idx = m1["timestamp"].searchsorted(entry_timestamp, side="right")
    history = m1.iloc[max(0, end_idx - (period + 1)):end_idx]
    if len(history) < 2:
        return 0.0
    prev_close = history["close"].shift(1)
    tr = pd.concat([
        history["high"] - history["low"],
        (history["high"] - prev_close).abs(),
        (history["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return float(tr.dropna().mean()) if not tr.dropna().empty else 0.0


def stop_ob_extreme(signal, entry_price, entry_timestamp, m1, context, config: StopLossConfig, pip_size: float) -> float:
    """The far boundary of the Order Block referenced by the signal (its
    `entry_zone`), plus a small buffer beyond it."""
    low, high = signal.entry_zone
    buffer = config.buffer_pips * pip_size
    return (low - buffer) if signal.direction == "bullish" else (high + buffer)


def stop_m5_structural(signal, entry_price, entry_timestamp, m1, context, config: StopLossConfig, pip_size: float) -> float:
    """Nearest M5 confirmed swing low (bullish) / swing high (bearish) at
    or before `entry_timestamp`."""
    buffer = config.buffer_pips * pip_size
    if context is None:
        return stop_ob_extreme(signal, entry_price, entry_timestamp, m1, context, config, pip_size)
    swings = context.swings("M5")
    swing_type = "low" if signal.direction == "bullish" else "high"
    subset = swings[(swings.swing_type == swing_type) & (swings.confirmed_timestamp <= entry_timestamp)]
    if subset.empty:
        return stop_ob_extreme(signal, entry_price, entry_timestamp, m1, context, config, pip_size)
    level = float(subset.iloc[-1]["price"])
    return (level - buffer) if signal.direction == "bullish" else (level + buffer)


def stop_fixed_pips(signal, entry_price, entry_timestamp, m1, context, config: StopLossConfig, pip_size: float) -> float:
    distance = config.fixed_pips * pip_size
    return entry_price - distance if signal.direction == "bullish" else entry_price + distance


def stop_atr_multiple(signal, entry_price, entry_timestamp, m1, context, config: StopLossConfig, pip_size: float) -> float:
    atr = _true_range_atr(m1, entry_timestamp, config.atr_period)
    distance = atr * config.atr_multiplier
    if distance <= 0:
        return stop_fixed_pips(signal, entry_price, entry_timestamp, m1, context, config, pip_size)
    return entry_price - distance if signal.direction == "bullish" else entry_price + distance


def stop_percentage(signal, entry_price, entry_timestamp, m1, context, config: StopLossConfig, pip_size: float) -> float:
    distance = entry_price * config.percentage
    return entry_price - distance if signal.direction == "bullish" else entry_price + distance


STOP_LOSS_METHODS = {
    "ob_extreme": stop_ob_extreme,
    "m5_structural": stop_m5_structural,
    "fixed_pips": stop_fixed_pips,
    "atr_multiple": stop_atr_multiple,
    "percentage": stop_percentage,
}


def resolve_stop_loss(signal, entry_price: float, entry_timestamp, m1: pd.DataFrame, context, config: StopLossConfig, pip_size: float) -> dict:
    method = STOP_LOSS_METHODS.get(config.method)
    if method is None:
        raise ValueError(f"Unknown stop-loss method: {config.method}")
    price = method(signal, entry_price, entry_timestamp, m1, context, config, pip_size)
    return {"stop_loss": price, "method": config.method}
