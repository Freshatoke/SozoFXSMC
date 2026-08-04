"""
Market Condition Classification.

Produces a per-candle (M1) classification of trending vs. ranging,
high vs. low volatility, and bull vs. bear directional bias, plus
gap-day flags (reusing Task 2's weekend-gap detection) and a documented
placeholder for news days (no external economic-calendar feed is wired
up yet -- see `is_news_day` below).

Every measure here is CAUSAL: each row's classification is computed from
a trailing rolling window ending at that row, never from future candles,
so labelling a trade by its entry timestamp (`label_trades_with_conditions`)
introduces no look-ahead.
"""

from __future__ import annotations

import pandas as pd

from src.features.reference_levels import compute_weekend_gaps


def _rolling_atr(m1: pd.DataFrame, period: int) -> pd.Series:
    prev_close = m1["close"].shift(1)
    tr = pd.concat([
        m1["high"] - m1["low"], (m1["high"] - prev_close).abs(), (m1["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=1).mean()


def classify_market_conditions(
    m1: pd.DataFrame, trend_window: int = 200, vol_window: int = 200,
    bull_bear_window: int = 1440, trend_efficiency_threshold: float = 0.3,
) -> pd.DataFrame:
    """Returns one row per input candle with:
        trend_state: "trending" | "ranging"
        volatility_state: "high" | "low"
        directional_bias: "bull" | "bear" | "neutral"
        is_gap_day: bool (this candle's calendar day immediately follows
            a detected weekend gap reopen)
        is_news_day: bool (always False -- see module docstring)
    """
    out = m1[["timestamp"]].copy()

    # Trend vs range: directional efficiency = |net move| / sum(|candle moves|)
    # over a trailing window. High efficiency -> most movement was in one
    # direction (trending); low efficiency -> lots of back-and-forth (ranging).
    net_move = (m1["close"] - m1["close"].shift(trend_window)).abs()
    path_length = m1["close"].diff().abs().rolling(trend_window, min_periods=1).sum()
    efficiency = (net_move / path_length.replace(0, pd.NA)).fillna(0.0)
    out["trend_efficiency"] = efficiency
    out["trend_state"] = efficiency.apply(lambda e: "trending" if e >= trend_efficiency_threshold else "ranging")

    # Volatility: current rolling ATR vs. its own longer-run median.
    atr = _rolling_atr(m1, vol_window)
    atr_median = atr.expanding(min_periods=vol_window).median()
    out["atr"] = atr
    out["volatility_state"] = (atr >= atr_median.fillna(atr)).map({True: "high", False: "low"})

    # Directional bias: trailing return over `bull_bear_window` candles.
    trailing_return = m1["close"] - m1["close"].shift(bull_bear_window)
    out["directional_bias"] = trailing_return.apply(lambda r: "bull" if r > 0 else ("bear" if r < 0 else "neutral"))

    # Gap days: mark every candle on the calendar day of a weekend reopen.
    gaps = compute_weekend_gaps(m1)
    gap_days = set(gaps["reopen_timestamp"].dt.date) if not gaps.empty else set()
    out["is_gap_day"] = out["timestamp"].dt.date.isin(gap_days)

    # News days: PLACEHOLDER. No economic-calendar feed is integrated in
    # this task -- wiring one up (e.g. a CSV of scheduled high-impact
    # release timestamps) is future work. Always False so downstream
    # research code can already group by this column without special-casing it.
    out["is_news_day"] = False

    return out


def label_trades_with_conditions(trades: list, conditions: pd.DataFrame) -> None:
    """Mutates each CLOSED trade's `metadata` in place with the market
    condition in effect at its `entry_timestamp` (as-of, via merge_asof --
    never a future condition row)."""
    if not trades:
        return
    lookup = conditions.sort_values("timestamp")
    for t in trades:
        if t.entry_timestamp is None:
            continue
        idx = lookup["timestamp"].searchsorted(t.entry_timestamp, side="right") - 1
        if idx < 0:
            continue
        row = lookup.iloc[idx]
        t.metadata["trend_state"] = row["trend_state"]
        t.metadata["volatility_state"] = row["volatility_state"]
        t.metadata["directional_bias"] = row["directional_bias"]
        t.metadata["is_gap_day"] = bool(row["is_gap_day"])
        t.metadata["is_news_day"] = bool(row["is_news_day"])
