"""
S5 -- Asia-to-London Liquidity Sweep.

Uses the Asian (Tokyo) session's high/low (src.features.sessions) as the
liquidity reference, looks for a sweep of that range during/after the
London session opens (the sweep condition is the same wick-beyond/close-
back rule used throughout this codebase), then requires a CHoCH
confirming the reversal (src.structure.market_structure) and a fresh
Order Block in the reversal direction (src.features.order_blocks), with
an optional FVG alignment check.

Direction: Asian HIGH swept -> BEARISH reversal. Asian LOW swept ->
BULLISH. `config.session_filter` (default `("london",)`) restricts the
entry candle to fall within the configured session(s).

Only the first qualifying candle per Asian session produces a signal.
"""

from __future__ import annotations

from config.settings import S5Config, DEFAULT_S5_CONFIG
from src.strategies.context import MarketContext
from src.strategies.common import Signal, compute_confidence, build_reason_codes, make_signal_id


def generate_signals(context: MarketContext, config: S5Config = DEFAULT_S5_CONFIG) -> list:
    if not config.enabled:
        return []

    signals = []
    seq = 0
    m1 = context.m1
    asian_sessions = context.sessions[context.sessions.session_name == "tokyo"].reset_index(drop=True)

    for i, asia in asian_sessions.iterrows():
        next_start = asian_sessions.iloc[i + 1]["start_utc"] if i + 1 < len(asian_sessions) else m1["timestamp"].iloc[-1]
        window = m1[(m1["timestamp"] > asia["end_utc"]) & (m1["timestamp"] <= next_start)]
        if window.empty:
            continue

        for level_value, sweep_side, direction in ((asia["high"], "above", "bearish"), (asia["low"], "below", "bullish")):
            swept_mask = (
                (window["high"] > level_value) & (window["close"] < level_value)
                if sweep_side == "above"
                else (window["low"] < level_value) & (window["close"] > level_value)
            )
            if not swept_mask.any():
                continue
            swept_ts = window.loc[swept_mask.idxmax(), "timestamp"]

            entry_window = m1[m1["timestamp"] > swept_ts]
            # Task 7.4: itertuples() over iterrows() -- see
            # s1_monday_gap.py's comment at the same pattern for why.
            for candle in entry_window.itertuples(index=False):
                t = candle.timestamp
                if t > next_start:
                    break  # don't leak into the next Asian session's own window

                if config.session_filter and not any(context.session_active_asof(s, t) for s in config.session_filter):
                    continue

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

                seq += 1
                entry_low, entry_high = (ob["low"], ob["high"]) if ob is not None else (candle.low, candle.high)
                stop_ref = level_value
                target = entry_high + (entry_high - entry_low) if direction == "bullish" else entry_low - (entry_high - entry_low)

                factor_values = {
                    "LiquiditySweep": 1.0,
                    "CHoCHConfirmation": 1.0,
                    "FreshOrderBlock": 1.0 if ob is not None else 0.0,
                    "FVGAlignment": 1.0 if fvg is not None else 0.0,
                    "SessionContext": 1.0,  # London-session filter already enforced above when configured
                }
                confidence, contributions = compute_confidence(factor_values)
                if confidence < config.confidence_threshold:
                    continue

                condition_codes = [
                    "AsianHighSwept" if direction == "bearish" else "AsianLowSwept",
                    "BullishCHoCH" if direction == "bullish" else "BearishCHoCH",
                ]
                if ob is not None:
                    condition_codes.append("BullishOrderBlock" if direction == "bullish" else "BearishOrderBlock")
                if fvg is not None:
                    condition_codes.append("BullishFVG" if direction == "bullish" else "BearishFVG")
                if config.session_filter:
                    condition_codes.append("LondonSession")
                reason_codes = build_reason_codes("S5", condition_codes, confidence)

                signal = Signal(
                    signal_id=make_signal_id("S5", context.symbol, "M1", t, seq),
                    strategy_id="S5",
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
                        "asian_high": asia["high"], "asian_low": asia["low"],
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
