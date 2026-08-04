"""
Take-Profit Engine: configurable target placement, one function per
method registered in `TAKE_PROFIT_METHODS`. Every method resolves the
PRIMARY final target price at entry time (never looking forward); partial
scaling out (`config.partial_exits`, a list of `(r_multiple, fraction)`
pairs) is layered on top uniformly regardless of method, producing an
ordered list of `(price, fraction)` levels nearest-first -- this is what
`Trade.take_profit_levels` stores and the engine consumes candle by
candle.

Methods that need data a given signal doesn't happen to carry (e.g.
`gap_fill_*` on a non-S1 signal, or `next_bos_target` on a signal with no
BOS confluence data) fall back to `fixed_rr` -- documented per-function.
"""

from __future__ import annotations

import pandas as pd

from config.settings import TakeProfitConfig


def _risk_distance(entry_price: float, stop_loss: float) -> float:
    return abs(entry_price - stop_loss)


def tp_fixed_rr(signal, entry_price, stop_loss, entry_timestamp, m1, context, config: TakeProfitConfig, pip_size: float) -> float:
    distance = _risk_distance(entry_price, stop_loss) * config.risk_reward
    return entry_price + distance if signal.direction == "bullish" else entry_price - distance


def tp_previous_high_low(signal, entry_price, stop_loss, entry_timestamp, m1, context, config: TakeProfitConfig, pip_size: float) -> float:
    if context is None:
        return tp_fixed_rr(signal, entry_price, stop_loss, entry_timestamp, m1, context, config, pip_size)
    ref = context.reference_levels
    level_type = "PDH" if signal.direction == "bullish" else "PDL"
    subset = ref[(ref.level_type == level_type) & (ref.available_from <= entry_timestamp)]
    if subset.empty:
        return tp_fixed_rr(signal, entry_price, stop_loss, entry_timestamp, m1, context, config, pip_size)
    level = float(subset.iloc[-1]["value"])
    beyond = (level > entry_price) if signal.direction == "bullish" else (level < entry_price)
    return level if beyond else tp_fixed_rr(signal, entry_price, stop_loss, entry_timestamp, m1, context, config, pip_size)


def tp_liquidity_level(signal, entry_price, stop_loss, entry_timestamp, m1, context, config: TakeProfitConfig, pip_size: float) -> float:
    if context is None:
        return tp_fixed_rr(signal, entry_price, stop_loss, entry_timestamp, m1, context, config, pip_size)
    liq = context.liquidity("M15")
    side = "buy_side" if signal.direction == "bullish" else "sell_side"
    subset = liq[(liq.side == side) & (liq.state == "ACTIVE") & (liq.creation_timestamp <= entry_timestamp)]
    subset = subset[subset.price > entry_price] if signal.direction == "bullish" else subset[subset.price < entry_price]
    if subset.empty:
        return tp_fixed_rr(signal, entry_price, stop_loss, entry_timestamp, m1, context, config, pip_size)
    return float(subset.price.min()) if signal.direction == "bullish" else float(subset.price.max())


def _gap_fill_target(signal, entry_price, stop_loss, entry_timestamp, m1, context, config, pip_size, fraction: float) -> float:
    meta = signal.metadata or {}
    friday_close = meta.get("friday_close")
    reopen_open = meta.get("reopen_open")
    if friday_close is None or reopen_open is None:
        return tp_fixed_rr(signal, entry_price, stop_loss, entry_timestamp, m1, context, config, pip_size)
    return reopen_open + fraction * (friday_close - reopen_open)


def tp_gap_fill_25(signal, entry_price, stop_loss, entry_timestamp, m1, context, config, pip_size):
    return _gap_fill_target(signal, entry_price, stop_loss, entry_timestamp, m1, context, config, pip_size, 0.25)


def tp_gap_fill_50(signal, entry_price, stop_loss, entry_timestamp, m1, context, config, pip_size):
    return _gap_fill_target(signal, entry_price, stop_loss, entry_timestamp, m1, context, config, pip_size, 0.50)


def tp_gap_fill_75(signal, entry_price, stop_loss, entry_timestamp, m1, context, config, pip_size):
    return _gap_fill_target(signal, entry_price, stop_loss, entry_timestamp, m1, context, config, pip_size, 0.75)


def tp_gap_fill_100(signal, entry_price, stop_loss, entry_timestamp, m1, context, config, pip_size):
    return _gap_fill_target(signal, entry_price, stop_loss, entry_timestamp, m1, context, config, pip_size, 1.00)


def tp_next_bos_target(signal, entry_price, stop_loss, entry_timestamp, m1, context, config: TakeProfitConfig, pip_size: float) -> float:
    snap = signal.confluence_snapshot or {}
    first_ts, second_ts = snap.get("first_bos_timestamp"), snap.get("second_bos_timestamp")
    if first_ts is None or second_ts is None or context is None:
        return tp_fixed_rr(signal, entry_price, stop_loss, entry_timestamp, m1, context, config, pip_size)
    events = context.structure_events("M15")
    first_row = events[events.break_candle_timestamp == first_ts]
    second_row = events[events.break_candle_timestamp == second_ts]
    if first_row.empty or second_row.empty:
        return tp_fixed_rr(signal, entry_price, stop_loss, entry_timestamp, m1, context, config, pip_size)
    leg = abs(float(second_row.iloc[0]["break_candle_close"]) - float(first_row.iloc[0]["break_candle_close"]))
    return entry_price + leg if signal.direction == "bullish" else entry_price - leg


TAKE_PROFIT_METHODS = {
    "fixed_rr": tp_fixed_rr,
    "previous_high_low": tp_previous_high_low,
    "liquidity_level": tp_liquidity_level,
    "gap_fill_25": tp_gap_fill_25,
    "gap_fill_50": tp_gap_fill_50,
    "gap_fill_75": tp_gap_fill_75,
    "gap_fill_100": tp_gap_fill_100,
    "next_bos_target": tp_next_bos_target,
}


def resolve_take_profit(signal, entry_price: float, stop_loss: float, entry_timestamp, m1: pd.DataFrame, context, config: TakeProfitConfig, pip_size: float) -> list:
    """Returns an ordered (nearest-to-entry-first) list of (price, fraction)
    levels summing to 1.0 across `fraction`."""
    method = TAKE_PROFIT_METHODS.get(config.method)
    if method is None:
        raise ValueError(f"Unknown take-profit method: {config.method}")
    final_price = method(signal, entry_price, stop_loss, entry_timestamp, m1, context, config, pip_size)

    risk_distance = _risk_distance(entry_price, stop_loss)
    levels = []
    allocated = 0.0
    for r_multiple, fraction in config.partial_exits:
        distance = risk_distance * r_multiple
        price = entry_price + distance if signal.direction == "bullish" else entry_price - distance
        levels.append((price, fraction))
        allocated += fraction

    remaining = round(1.0 - allocated, 6)
    if remaining > 0:
        levels.append((final_price, remaining))

    levels.sort(key=lambda lv: abs(lv[0] - entry_price))
    return levels
