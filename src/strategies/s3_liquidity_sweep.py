"""
S3 -- Liquidity Sweep Reversal.

Detects: a swept liquidity level (src.features.liquidity), followed by
displacement in the reversal direction (src.features.displacement),
followed by a CHoCH confirming the reversal (src.structure.market_structure),
followed by a fresh Order Block in the reversal direction
(src.features.order_blocks), with an optional FVG alignment check.

Direction: a swept BUY-SIDE (above-price) level implies a BEARISH reversal
signal (the sweep exhausted buyers' stops); a swept SELL-SIDE level
implies BULLISH.

Only the first qualifying candle per swept level produces a signal.
"""

from __future__ import annotations

from config.settings import S3Config, DEFAULT_S3_CONFIG
from src.strategies.context import MarketContext
from src.strategies.common import Signal, compute_confidence, build_reason_codes, make_signal_id


def generate_signals(context: MarketContext, config: S3Config = DEFAULT_S3_CONFIG) -> list:
    if not config.enabled:
        return []

    signals = []
    seq = 0
    m1 = context.m1
    liquidity = context.liquidity(config.ob_timeframe)
    swept = liquidity[liquidity.swept_timestamp.notna()]

    for _, level in swept.iterrows():
        reversal_direction = "bearish" if level["side"] == "buy_side" else "bullish"
        swept_ts = level["swept_timestamp"]

        displacement_events = context.displacement(config.ob_timeframe)
        disp_after = displacement_events[
            (displacement_events.direction == reversal_direction) & (displacement_events.start_timestamp >= swept_ts)
        ]
        if config.require_displacement and disp_after.empty:
            continue
        displacement_ref = disp_after.iloc[0] if not disp_after.empty else None

        window = m1[m1["timestamp"] > swept_ts]
        # Task 7.4: itertuples() over iterrows() -- see s1_monday_gap.py's
        # comment at the same pattern for why.
        for candle in window.itertuples(index=False):
            t = candle.timestamp

            choch = context.latest_choch_asof(config.choch_timeframe, t, direction=reversal_direction)
            if choch is None or choch["break_candle_timestamp"] < swept_ts:
                continue
            if displacement_ref is not None and choch["break_candle_timestamp"] < displacement_ref["start_timestamp"]:
                continue

            ob = context.fresh_order_block_asof(config.ob_timeframe, reversal_direction, t)
            if config.require_fresh_ob and ob is None:
                continue

            # Task 11 Phase 1 fix: always look up the FVG (cheap, point-in-time
            # lookup) so FVGAlignment reflects reality even when not required
            # -- previously this was only computed when require_fvg=True,
            # discarding real information and scoring a flat neutral 0.5
            # regardless of whether an FVG actually existed. Entry-gating
            # behavior below is unchanged.
            fvg = context.active_fvg_asof(config.ob_timeframe, reversal_direction, t)
            if config.require_fvg and fvg is None:
                continue

            if config.session_filter and not any(context.session_active_asof(s, t) for s in config.session_filter):
                continue

            seq += 1
            entry_low, entry_high = (ob["low"], ob["high"]) if ob is not None else (candle.low, candle.high)
            stop_ref = level["price"]  # the swept extreme itself
            liq_side = "sell_side" if reversal_direction == "bullish" else "buy_side"
            opposite = liquidity[(liquidity.side == ("buy_side" if liq_side == "sell_side" else "sell_side"))
                                  & (liquidity.state == "ACTIVE") & (liquidity.creation_timestamp <= t)]
            target = None
            if not opposite.empty:
                target = float(opposite.price.max()) if reversal_direction == "bullish" else float(opposite.price.min())
            if target is None:
                target = entry_high + (entry_high - entry_low) if reversal_direction == "bullish" else entry_low - (entry_high - entry_low)

            factor_values = {
                "LiquiditySweep": 1.0,
                "CHoCHConfirmation": 1.0,
                "DisplacementQuality": 1.0 if displacement_ref is not None else 0.0,
                "FreshOrderBlock": 1.0 if ob is not None else 0.0,
                "FVGAlignment": 1.0 if fvg is not None else 0.0,
            }
            confidence, contributions = compute_confidence(factor_values)
            if confidence < config.confidence_threshold:
                continue

            condition_codes = ["LiquiditySwept", "BullishCHoCH" if reversal_direction == "bullish" else "BearishCHoCH"]
            if displacement_ref is not None:
                condition_codes.append("DisplacementConfirmed")
            if ob is not None:
                condition_codes.append("BullishOrderBlock" if reversal_direction == "bullish" else "BearishOrderBlock")
            if fvg is not None:
                condition_codes.append("FVGAligned")
            reason_codes = build_reason_codes("S3", condition_codes, confidence)

            signal = Signal(
                signal_id=make_signal_id("S3", context.symbol, "M1", t, seq),
                strategy_id="S3",
                timestamp=t,
                symbol=context.symbol,
                timeframe="M1",
                direction=reversal_direction,
                entry_zone=(entry_low, entry_high),
                stop_loss_reference=stop_ref,
                target_reference=target,
                confidence_score=confidence,
                reason_codes=reason_codes,
                confluence_snapshot={
                    "liquidity_id": level["liquidity_id"],
                    "swept_timestamp": swept_ts,
                    "choch_timestamp": choch["break_candle_timestamp"],
                    "order_block_id": ob["ob_id"] if ob is not None else None,
                },
                market_structure_state=context.structure_state_asof(config.choch_timeframe, t),
                session=next((s for s in context.session_config.windows.keys() if context.session_active_asof(s, t)), None),
                risk_reference={"type": config.stop_reference, "value": stop_ref},
                metadata={
                    "liquidity_type": level["type"], "liquidity_side": level["side"],
                    "displacement_id": displacement_ref["displacement_id"] if displacement_ref is not None else None,
                    "confidence_contributions": contributions,
                },
            )
            signals.append(signal)
            break

    return signals
