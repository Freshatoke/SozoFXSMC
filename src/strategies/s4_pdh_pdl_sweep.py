"""
S4 -- Previous Day High/Low Sweep.

Detects a sweep of PDH or PDL (levels come from
src.features.reference_levels; the sweep condition itself -- a wick
beyond the level with the candle CLOSING back on the origin side -- is
the same wick-based rule already defined by src.features.liquidity for
swing-based levels, applied here to PDH/PDL since Task 2 does not itself
compute PDH/PDL sweep events), followed by a CHoCH confirming the
reversal (src.structure.market_structure) and a fresh Order Block in the
reversal direction (src.features.order_blocks).

Direction: PDH swept -> BEARISH reversal signal. PDL swept -> BULLISH.

Only the first qualifying candle per (PDH/PDL, reference day) produces a
signal.
"""

from __future__ import annotations

import pandas as pd

from config.settings import S4Config, DEFAULT_S4_CONFIG
from src.strategies.context import MarketContext
from src.strategies.common import Signal, compute_confidence, build_reason_codes, make_signal_id


def generate_signals(context: MarketContext, config: S4Config = DEFAULT_S4_CONFIG) -> list:
    if not config.enabled:
        return []

    signals = []
    seq = 0
    m1 = context.m1
    ref_levels = context.reference_levels

    for level_type, direction in (("PDH", "bearish"), ("PDL", "bullish")):
        rows = ref_levels[ref_levels.level_type == level_type].reset_index(drop=True)
        for i, level in rows.iterrows():
            available_from = level["available_from"]
            next_available = rows.iloc[i + 1]["available_from"] if i + 1 < len(rows) else m1["timestamp"].iloc[-1] + pd.Timedelta(days=1)
            window = m1[(m1["timestamp"] > available_from) & (m1["timestamp"] <= next_available)]
            if window.empty:
                continue

            sweep_direction = "above" if level_type == "PDH" else "below"
            swept_mask = (
                (window["high"] > level["value"]) & (window["close"] < level["value"])
                if sweep_direction == "above"
                else (window["low"] < level["value"]) & (window["close"] > level["value"])
            )
            if not swept_mask.any():
                continue
            swept_ts = window.loc[swept_mask.idxmax(), "timestamp"]

            entry_window = m1[m1["timestamp"] > swept_ts]
            # Task 7.4: itertuples() over iterrows() -- see
            # s1_monday_gap.py's comment at the same pattern for why.
            for candle in entry_window.itertuples(index=False):
                t = candle.timestamp

                choch = context.latest_choch_asof(config.choch_timeframe, t, direction=direction)
                if choch is None or choch["break_candle_timestamp"] < swept_ts:
                    continue

                ob = context.fresh_order_block_asof(config.ob_timeframe, direction, t)
                if config.require_fresh_ob and ob is None:
                    continue

                # Task 11 Phase 1 fix: always look up the FVG (cheap,
                # point-in-time lookup) so FVGAlignment reflects reality
                # even when not required -- previously only computed when
                # require_fvg=True, discarding real information and
                # scoring a flat neutral 0.5 regardless of whether an FVG
                # actually existed. Entry-gating behavior below is unchanged.
                fvg = context.active_fvg_asof(config.ob_timeframe, direction, t)
                if config.require_fvg and fvg is None:
                    continue

                if config.session_filter and not any(context.session_active_asof(s, t) for s in config.session_filter):
                    continue

                seq += 1
                entry_low, entry_high = (ob["low"], ob["high"]) if ob is not None else (candle.low, candle.high)
                stop_ref = level["value"]
                target = entry_high + (entry_high - entry_low) if direction == "bullish" else entry_low - (entry_high - entry_low)

                factor_values = {
                    "LiquiditySweep": 1.0,
                    "CHoCHConfirmation": 1.0,
                    "FreshOrderBlock": 1.0 if ob is not None else 0.0,
                    "FVGAlignment": 1.0 if fvg is not None else 0.0,
                }
                confidence, contributions = compute_confidence(factor_values)
                if confidence < config.confidence_threshold:
                    continue

                condition_codes = [f"{level_type}Swept", "BullishCHoCH" if direction == "bullish" else "BearishCHoCH"]
                if ob is not None:
                    condition_codes.append("BullishOrderBlock" if direction == "bullish" else "BearishOrderBlock")
                if fvg is not None:
                    condition_codes.append("FVGAligned")
                reason_codes = build_reason_codes("S4", condition_codes, confidence)

                signal = Signal(
                    signal_id=make_signal_id("S4", context.symbol, "M1", t, seq),
                    strategy_id="S4",
                    timestamp=t,
                    symbol=context.symbol,
                    timeframe="M1",
                    direction=direction,
                    entry_zone=(entry_low, entry_high),
                    stop_loss_reference=stop_ref,
                    target_reference=target,
                    confidence_score=confidence,
                    reason_codes=reason_codes,
                    confluence_snapshot={
                        "level_type": level_type, "level_value": level["value"],
                        "swept_timestamp": swept_ts, "choch_timestamp": choch["break_candle_timestamp"],
                        "order_block_id": ob["ob_id"] if ob is not None else None,
                    },
                    market_structure_state=context.structure_state_asof(config.choch_timeframe, t),
                    session=next((s for s in context.session_config.windows.keys() if context.session_active_asof(s, t)), None),
                    risk_reference={"type": config.stop_reference, "value": stop_ref},
                    metadata={"confidence_contributions": contributions},
                )
                signals.append(signal)
                break
    return signals
