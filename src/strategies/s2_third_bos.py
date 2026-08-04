"""
S2 -- Third BOS Continuation.

Research question: after two consecutive same-direction BOS events on
`config.bos_timeframe` (default M15), does price show a statistically
meaningful tendency to produce a THIRD continuation BOS following an
appropriate retracement and lower-timeframe confirmation?

"Two consecutive BOS in the same direction" = two adjacent events in the
`MarketContext.structure_events(bos_timeframe)` stream that are both
`event_type == "BOS"` with the same `direction` (the structure engine's
own state machine already guarantees no opposing CHoCH occurred between
them, since a direction change would have produced a CHoCH, not a BOS --
see docs/MARKET_STRUCTURE_SPEC.md).

Entry sequence after the second BOS (all at/before candle T):
    1. A retracement Order Block (fresh, in the continuation direction)
       forms after the second BOS.
    2. An M5 CHoCH in the continuation direction confirms the retracement
       is over.
    3. An M1 CHoCH in the continuation direction is confirmed exactly at T.

Whether BOS #3 actually occurs afterward is recorded in `metadata` as a
post-hoc research outcome -- it is never used to decide whether the
signal fires (that would be look-ahead by definition).
"""

from __future__ import annotations

from config.settings import S2Config, DEFAULT_S2_CONFIG
from src.strategies.context import MarketContext
from src.strategies.common import Signal, compute_confidence, build_reason_codes, make_signal_id


def _find_bos_pairs(events):
    pairs = []
    events = events.reset_index(drop=True)
    for i in range(len(events) - 1):
        a, b = events.iloc[i], events.iloc[i + 1]
        if a["event_type"] == "BOS" and b["event_type"] == "BOS" and a["direction"] == b["direction"]:
            pairs.append((a, b))
    return pairs


def _measured_move_target(first_bos, second_bos, entry_price: float, direction: str) -> float:
    leg = abs(second_bos["break_candle_close"] - first_bos["break_candle_close"])
    return entry_price + leg if direction == "bullish" else entry_price - leg


def _liquidity_target(context: MarketContext, timeframe: str, direction: str, price: float, timestamp) -> float | None:
    liq = context.liquidity(timeframe)
    side = "buy_side" if direction == "bullish" else "sell_side"
    subset = liq[(liq.side == side) & (liq.state == "ACTIVE") & (liq.creation_timestamp <= timestamp)]
    subset = subset[subset.price > price] if direction == "bullish" else subset[subset.price < price]
    if subset.empty:
        return None
    return float(subset.price.min()) if direction == "bullish" else float(subset.price.max())


def _bos3_occurred(context: MarketContext, timeframe: str, direction: str, after_timestamp):
    events = context.structure_events(timeframe)
    subset = events[
        (events.event_type == "BOS") & (events.direction == direction) & (events.break_candle_timestamp > after_timestamp)
    ]
    if subset.empty:
        return False, None
    return True, subset.iloc[0]["break_candle_timestamp"]


def generate_signals(context: MarketContext, config: S2Config = DEFAULT_S2_CONFIG) -> list:
    if not config.enabled:
        return []

    signals = []
    seq = 0
    m1 = context.m1
    bos_events = context.structure_events(config.bos_timeframe)
    pairs = _find_bos_pairs(bos_events)

    for first_bos, second_bos in pairs:
        direction = second_bos["direction"]
        after_ts = second_bos["break_candle_timestamp"]

        window = m1[m1["timestamp"] > after_ts]
        # Task 7.4: itertuples() over iterrows() -- see s1_monday_gap.py's
        # comment at the same pattern for why.
        for candle in window.itertuples(index=False):
            t = candle.timestamp

            ob = context.fresh_order_block_asof(config.ob_timeframe, direction, t)
            if ob is None or ob["creation_timestamp"] <= after_ts:
                continue
            if config.require_fresh_ob and ob is None:
                continue

            m5_choch = context.latest_choch_asof(config.choch_timeframe, t, direction=direction)
            if m5_choch is None or m5_choch["break_candle_timestamp"] <= after_ts:
                continue

            m1_choch = context.latest_choch_asof(config.entry_choch_timeframe, t, direction=direction)
            if m1_choch is None or m1_choch["break_candle_timestamp"] != t:
                continue
            if m1_choch["break_candle_timestamp"] <= m5_choch["break_candle_timestamp"]:
                continue

            # Task 11 Phase 1 fix: always look up the FVG (cheap, point-in-time
            # lookup) so FVGAlignment reflects reality even when not required
            # -- previously this was only computed when require_fvg=True,
            # so the "not required" case discarded real information and
            # scored a flat neutral 0.5 regardless of whether an FVG
            # actually existed. The entry-gating behavior below is unchanged.
            fvg = context.active_fvg_asof(config.ob_timeframe, direction, t)
            if config.require_fvg and fvg is None:
                continue

            if config.session_filter and not any(context.session_active_asof(s, t) for s in config.session_filter):
                continue

            seq += 1
            entry_price = candle.close
            if config.target_style == "liquidity":
                target = _liquidity_target(context, config.ob_timeframe, direction, entry_price, t)
                if target is None:
                    target = _measured_move_target(first_bos, second_bos, entry_price, direction)
            else:
                target = _measured_move_target(first_bos, second_bos, entry_price, direction)

            stop_ref = ob["low"] if direction == "bullish" else ob["high"]

            factor_values = {
                "CHoCHConfirmation": 1.0,
                "FreshOrderBlock": 1.0,
                "TrendAlignment": 1.0,  # by construction: two prior BOS in this exact direction
                "FVGAlignment": 1.0 if fvg is not None else 0.0,
                "SessionContext": 1.0 if (not config.session_filter or any(context.session_active_asof(s, t) for s in config.session_filter)) else 0.0,
            }
            confidence, contributions = compute_confidence(factor_values)
            if confidence < config.confidence_threshold:
                continue

            condition_codes = ["FirstBOS", "SecondBOS", "BullishCHoCH" if direction == "bullish" else "BearishCHoCH",
                                "BullishOrderBlock" if direction == "bullish" else "BearishOrderBlock"]
            if fvg is not None:
                condition_codes.append("FVGAligned")
            reason_codes = build_reason_codes("S2", condition_codes, confidence)

            bos3, bos3_ts = _bos3_occurred(context, config.bos_timeframe, direction, second_bos["break_candle_timestamp"])

            signal = Signal(
                signal_id=make_signal_id("S2", context.symbol, "M1", t, seq),
                strategy_id="S2",
                timestamp=t,
                symbol=context.symbol,
                timeframe="M1",
                direction=direction,
                entry_zone=(ob["low"], ob["high"]),
                stop_loss_reference=stop_ref,
                target_reference=target,
                confidence_score=confidence,
                reason_codes=reason_codes,
                confluence_snapshot={
                    "first_bos_timestamp": first_bos["break_candle_timestamp"],
                    "second_bos_timestamp": second_bos["break_candle_timestamp"],
                    "m5_choch_timestamp": m5_choch["break_candle_timestamp"],
                    "m1_choch_timestamp": m1_choch["break_candle_timestamp"],
                    "order_block_id": ob["ob_id"],
                },
                market_structure_state=context.structure_state_asof(config.entry_choch_timeframe, t),
                session=next((s for s in context.session_config.windows.keys() if context.session_active_asof(s, t)), None),
                risk_reference={"type": config.stop_reference, "value": stop_ref},
                metadata={
                    "target_style": config.target_style,
                    "bos3_occurred": bos3,
                    "bos3_timestamp": bos3_ts,
                    "confidence_contributions": contributions,
                },
            )
            signals.append(signal)
            break  # one signal per BOS pair

    return signals
