"""
S1 -- Monday Gap Reversion.

Research question: does price retrace toward the Friday close after the
weekend gap, once SMC structure confirms a reversal?

This module detects nothing itself beyond sequencing already-detected
objects: the weekend gap comes from `MarketContext.weekend_gaps`
(src.features.reference_levels), CHoCH from `MarketContext.structure_events`
(src.structure.market_structure), Order Blocks from
`MarketContext.order_blocks` (src.features.order_blocks), and engulfing
from `MarketContext.engulfing` (src.features.engulfing).

Entry sequence (all must hold, in this temporal order, at candle T):
    1. A weekend gap exists whose size >= config.min_gap_size.
    2. Price has begun moving back toward the Friday close (a simple
       "closed on the reversal side of the reopen price" check).
    3. An M5 CHoCH in the reversal direction occurred after the gap reopened.
    4. An M1 CHoCH in the reversal direction is confirmed exactly at T
       (this is what selects T as the unique entry candle).
    5. A FRESH Order Block in the reversal direction exists at/before T.
    6. (optional, config.require_engulfing) an engulfing candle in the
       reversal direction occurred within the last 2 M1 candles.

Only the FIRST M1 candle satisfying all of the above per gap produces a
signal -- this is what prevents duplicate signals for the same gap.

No look-ahead: every condition is evaluated using only context data whose
own timestamp is <= T (MarketContext's helpers already enforce this by
construction -- see context.py). The only exception is the descriptive,
research-only fields on gap fill checkpoints / MAE / MFE / time-to-fill,
which are computed by looking forward from T -- these are OUTCOME
statistics recorded for research, never used to decide whether the signal
fires (the entry decision is made using only information available at T).
"""

from __future__ import annotations

import pandas as pd

from config.settings import S1Config, DEFAULT_S1_CONFIG
from src.strategies.context import MarketContext
from src.strategies.common import Signal, compute_confidence, build_reason_codes, make_signal_id


def _gap_fill_checkpoints(m1: pd.DataFrame, gap_row, reversal_direction: str) -> dict:
    """Post-hoc (outcome) statistics: timestamps at which 25/50/75/100% of
    the gap had been retraced, computed by scanning forward from the gap
    reopen. Purely descriptive -- never used as an entry condition."""
    window = m1[m1["timestamp"] > gap_row["reopen_timestamp"]]
    friday_close = gap_row["friday_close"]
    reopen_open = gap_row["reopen_open"]
    gap_size = abs(reopen_open - friday_close)
    checkpoints = {"25%": None, "50%": None, "75%": None, "100%": None}
    if gap_size == 0 or window.empty:
        return checkpoints

    if reversal_direction == "bearish":  # gap up, price must fall back toward friday_close
        progress = (reopen_open - window["low"]).clip(lower=0) / gap_size
    else:  # bullish reversal, gap down, price must rise back toward friday_close
        progress = (window["high"] - reopen_open).clip(lower=0) / gap_size

    cum_max = progress.cummax()
    for label, threshold in (("25%", 0.25), ("50%", 0.50), ("75%", 0.75), ("100%", 1.0)):
        hit = cum_max[cum_max >= threshold]
        if not hit.empty:
            checkpoints[label] = window.loc[hit.index[0], "timestamp"]
    return checkpoints


def _mae_mfe(m1: pd.DataFrame, entry_timestamp, entry_price: float, direction: str, horizon_timestamp) -> dict:
    """Post-hoc (outcome) Maximum Adverse/Favourable Excursion between
    entry and `horizon_timestamp` (or end of data). Research-only."""
    window = m1[(m1["timestamp"] > entry_timestamp) & (m1["timestamp"] <= horizon_timestamp)] if horizon_timestamp is not None \
        else m1[m1["timestamp"] > entry_timestamp]
    if window.empty:
        return {"mae": 0.0, "mfe": 0.0}
    if direction == "bullish":
        mfe = float((window["high"].max() - entry_price))
        mae = float((entry_price - window["low"].min()))
    else:
        mfe = float((entry_price - window["low"].min()))
        mae = float((window["high"].max() - entry_price))
    return {"mae": max(0.0, mae), "mfe": max(0.0, mfe)}


def generate_signals(context: MarketContext, config: S1Config = DEFAULT_S1_CONFIG) -> list:
    if not config.enabled:
        return []

    signals = []
    seq = 0
    m1 = context.m1

    for _, gap in context.weekend_gaps.iterrows():
        gap_size = abs(gap["gap_size"])
        if gap_size < config.min_gap_size or gap["gap_direction"] == "flat":
            continue

        reversal_direction = "bearish" if gap["gap_direction"] == "up" else "bullish"
        reopen_open = gap["reopen_open"]
        friday_close = gap["friday_close"]

        window = m1[m1["timestamp"] > gap["reopen_timestamp"]]
        # Task 7.4: .itertuples() instead of .iterrows() -- iterrows()
        # constructs a full pandas Series per row (dtype inference, index
        # alignment, ...), which profiling showed dominates runtime on
        # multi-year data; itertuples() returns a plain namedtuple with
        # none of that overhead. Same values, same order, same behavior.
        for candle in window.itertuples(index=False):
            t = candle.timestamp

            moving_toward_close = candle.close < reopen_open if reversal_direction == "bearish" else candle.close > reopen_open
            if not moving_toward_close:
                continue

            m5_choch = context.latest_choch_asof(config.choch_timeframe, t, direction=reversal_direction)
            if m5_choch is None or m5_choch["break_candle_timestamp"] <= gap["reopen_timestamp"]:
                continue

            m1_choch = context.latest_choch_asof(config.entry_choch_timeframe, t, direction=reversal_direction)
            if m1_choch is None or m1_choch["break_candle_timestamp"] != t:
                continue
            if m1_choch["break_candle_timestamp"] <= m5_choch["break_candle_timestamp"]:
                continue

            ob = context.fresh_order_block_asof(config.ob_timeframe, reversal_direction, t)
            if config.require_fresh_ob and ob is None:
                continue

            engulfing_ok = True
            if config.require_engulfing:
                recent_engulf = context.engulfing[
                    (context.engulfing.direction == reversal_direction)
                    & (context.engulfing.timestamp <= t)
                    & (context.engulfing.timestamp > t - pd.Timedelta(minutes=2))
                ]
                engulfing_ok = not recent_engulf.empty
                if not engulfing_ok:
                    continue

            if config.session_filter and not any(context.session_active_asof(s, t) for s in config.session_filter):
                continue

            # --- build the signal ---
            seq += 1
            entry_low, entry_high = (ob["low"], ob["high"]) if ob is not None else (candle.low, candle.high)
            stop_ref = ob["high"] if (ob is not None and reversal_direction == "bearish") else (
                ob["low"] if ob is not None else candle.low
            )
            target_ref = friday_close

            gap_quality = min(gap_size / (config.min_gap_size * 3), 1.0) if config.min_gap_size > 0 else 0.5
            factor_values = {
                "GapQuality": gap_quality,
                "CHoCHConfirmation": 1.0,
                "FreshOrderBlock": 1.0 if ob is not None else 0.0,
                "Engulfing": 1.0 if (config.require_engulfing and engulfing_ok) else (0.5 if not config.require_engulfing else 0.0),
                "SessionContext": 1.0 if (not config.session_filter or any(context.session_active_asof(s, t) for s in config.session_filter)) else 0.0,
            }
            confidence, contributions = compute_confidence(factor_values)
            if confidence < config.confidence_threshold:
                continue

            condition_codes = ["GapDetected", "GapAboveMinimum"]
            condition_codes.append("BullishCHoCH" if reversal_direction == "bullish" else "BearishCHoCH")
            if ob is not None:
                condition_codes.append("BullishOrderBlock" if reversal_direction == "bullish" else "BearishOrderBlock")
            if config.require_engulfing and engulfing_ok:
                condition_codes.append("BullishEngulfing" if reversal_direction == "bullish" else "BearishEngulfing")
            reason_codes = build_reason_codes("S1", condition_codes, confidence)

            checkpoints = _gap_fill_checkpoints(m1, gap, reversal_direction)
            fill_100_ts = checkpoints["100%"]
            time_to_fill = (fill_100_ts - gap["reopen_timestamp"]) if fill_100_ts is not None else None
            mae_mfe = _mae_mfe(m1, t, candle.close, reversal_direction, fill_100_ts)

            active_session = next((s for s in (context.session_config.windows.keys()) if context.session_active_asof(s, t)), None)

            signal = Signal(
                signal_id=make_signal_id("S1", context.symbol, "M1", t, seq),
                strategy_id="S1",
                timestamp=t,
                symbol=context.symbol,
                timeframe="M1",
                direction=reversal_direction,
                entry_zone=(entry_low, entry_high),
                stop_loss_reference=stop_ref,
                target_reference=target_ref,
                confidence_score=confidence,
                reason_codes=reason_codes,
                confluence_snapshot={
                    "m5_choch_timestamp": m5_choch["break_candle_timestamp"],
                    "m1_choch_timestamp": m1_choch["break_candle_timestamp"],
                    "order_block_id": ob["ob_id"] if ob is not None else None,
                },
                market_structure_state=context.structure_state_asof(config.entry_choch_timeframe, t),
                session=active_session,
                risk_reference={"type": config.stop_reference, "value": stop_ref},
                metadata={
                    "gap_id": gap["gap_id"],
                    "friday_close": friday_close,
                    "reopen_open": reopen_open,
                    "gap_size": gap["gap_size"],
                    "gap_pct": gap["gap_pct"],
                    "gap_direction": gap["gap_direction"],
                    "gap_age_candles_at_reopen": gap["gap_age_candles"],
                    "fill_checkpoints": checkpoints,
                    "time_to_fill": time_to_fill,
                    "mae": mae_mfe["mae"],
                    "mfe": mae_mfe["mfe"],
                    "confidence_contributions": contributions,
                },
            )
            signals.append(signal)
            break  # one signal per gap: stop scanning further candles for this gap

    return signals
