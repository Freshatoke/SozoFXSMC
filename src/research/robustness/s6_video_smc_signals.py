"""
Task 12.1 Phase 5 -- S6: Video SMC Research Strategy, RESEARCH-ONLY.

Reconstructs the trend-continuation core of the video "Revealing my Full
SMC Trading Strategy (Live Trade Walkthrough)" using ONLY primitives that
already exist in this codebase (`MarketContext`) -- no volume profile, no
DXY correlation, no discretionary "buyer/seller battle" reading, since
none of those are reproducible with this platform's data/features (see
docs/VIDEO_SMC_STRATEGY_SPECIFICATION.md's Phase 4 "not sufficiently
specified" list for exactly what was dropped and why).

Deliberately SEPARATE from S1-S5, IOS, ITQS, the Decision Engine, and the
live scanner -- imported and run only from this research package and
`scripts/run_s6_video_smc_experiment.py`.

Formalization (see docs/VIDEO_SMC_STRATEGY_SPECIFICATION.md for the full
RULE/INPUT/TIMEFRAME/CONDITION/ENTRY/STOP/TARGET/INVALIDATION breakdown):

    HTF bias      = M15 structure_state (video's "15-minute general drift")
    Location      = a fresh Order Block AND/OR active FVG on M15 in the
                    bias direction (proxy for the video's volume-profile
                    "high-node battle zone" -- the closest existing concept
                    in this codebase to "a level where price previously
                    reversed")
    Confirmation  = an M1 CHoCH in the bias direction, occurring strictly
                    after the location's creation (video's "short-term
                    aligned with the long-term... that is your signal to go")
    Entry         = at the FVG zone if present, else the OB zone (video:
                    "I placed my long position also in the gap")
    Stop          = beyond the location's low/high (video: "my stop loss
                    going just at the low of this level")
    Management    = NOT a fixed single target -- backtested with
                    TakeProfitConfig.partial_exits (an ALREADY-EXISTING
                    execution-engine feature, not new code) to approximate
                    the video's staged partial-exit + trailing-stop
                    management, the strategy's most genuinely distinctive
                    contribution relative to S1-S5's mostly single-target
                    defaults.

Deliberately LOOSER than S2 (Third BOS Continuation): S2 requires two
CONSECUTIVE same-direction BOS events before considering a continuation
entry; S6 fires on the CURRENT structure state alone (the video's bias
step is just "is the market structure bullish or bearish right now,"
not a specific historical pattern) -- this is an intentional, documented
design choice to test whether a higher-frequency, less-selective variant
of the same underlying idea is a useful complement, per this task's
explicit framing.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional

from src.strategies.common import Signal, compute_confidence, build_reason_codes, make_signal_id, dedupe_signals


@dataclass(frozen=True)
class S6Config:
    enabled: bool = True
    bias_timeframe: str = "M15"          # HTF "general drift"
    entry_choch_timeframe: str = "M1"    # LTF "immediate drift" realignment
    ob_timeframe: str = "M15"
    fvg_timeframe: str = "M15"
    require_ob: bool = False             # location may be satisfied by OB, FVG, or both
    require_fvg: bool = False
    require_both_ob_and_fvg: bool = False
    ob_min_quality: float = 0.0
    fvg_min_size_pct: float = 0.0
    session_filter: Optional[tuple] = None
    day_of_week_filter: Optional[tuple] = None
    confidence_threshold: float = 0.0
    max_lookout_candles: int = 300   # ~5h of M1 -- bounds each trigger event's entry search (see generate_signals docstring)

    def param_dict(self) -> dict:
        return asdict(self)


def generate_signals(context, config: S6Config = S6Config()) -> list:
    if not config.enabled:
        return []

    signals = []
    seq = 0
    m1 = context.m1
    if m1.empty:
        return signals

    for direction in ("bullish", "bearish"):
        # Trigger event: every timestamp at which the HTF structure state
        # ALREADY equals this direction -- approximated cheaply by scanning
        # M5 CHoCH/BOS events in this direction (each one marks a point
        # where the HTF bias was freshly (re)confirmed) rather than
        # scanning every M1 candle, matching S2/S3/S4's existing
        # per-event iteration pattern for performance.
        events = context.structure_events(config.bias_timeframe).sort_values("break_candle_timestamp").reset_index(drop=True)
        direction_events = events[events.direction == direction]
        opposite_events = events[events.direction != direction]["break_candle_timestamp"]

        for _, event in direction_events.iterrows():
            after_ts = event["break_candle_timestamp"]

            # Bound the search to WHILE this trend regime is still intact:
            # the next opposite-direction structure event ends it (a
            # continuation setup makes no sense once the trend itself has
            # reversed), with max_lookout_candles as a hard safety cap so
            # a single unusually long trend regime can never make one
            # trigger event scan an unbounded amount of the dataset --
            # this is what an earlier, uncapped version of this loop got
            # wrong (see git history / Task 12.1 completion report).
            later_opposite = opposite_events[opposite_events > after_ts]
            regime_end = later_opposite.min() if not later_opposite.empty else m1["timestamp"].iloc[-1]

            window = m1[(m1["timestamp"] > after_ts) & (m1["timestamp"] <= regime_end)].head(config.max_lookout_candles)
            for candle in window.itertuples(index=False):
                t = candle.timestamp

                if config.day_of_week_filter is not None and t.dayofweek not in config.day_of_week_filter:
                    continue
                if config.session_filter and not any(context.session_active_asof(s, t) for s in config.session_filter):
                    continue

                m1_choch = context.latest_choch_asof(config.entry_choch_timeframe, t, direction=direction)
                if m1_choch is None or m1_choch["break_candle_timestamp"] != t:
                    continue
                if m1_choch["break_candle_timestamp"] <= after_ts:
                    continue

                ob = context.fresh_order_block_asof(config.ob_timeframe, direction, t)
                if ob is not None and ob.get("quality_score") is not None and ob["quality_score"] < config.ob_min_quality:
                    ob = None
                fvg = context.active_fvg_asof(config.fvg_timeframe, direction, t)
                if fvg is not None and config.fvg_min_size_pct > 0:
                    fvg_size_pct = (fvg["size"] / candle.close * 100.0) if candle.close else 0.0
                    if fvg_size_pct < config.fvg_min_size_pct:
                        fvg = None

                if config.require_both_ob_and_fvg and (ob is None or fvg is None):
                    continue
                if config.require_ob and ob is None:
                    continue
                if config.require_fvg and fvg is None:
                    continue
                if ob is None and fvg is None:
                    continue  # "location" (Phase 1's step 2) is mandatory: SOME zone must exist

                # Entry: FVG zone preferred (video's literal "entry was the
                # gap"), Order Block zone as fallback.
                entry_low, entry_high = (fvg["bottom"], fvg["top"]) if fvg is not None else (ob["low"], ob["high"])
                stop_ref = entry_low if direction == "bullish" else entry_high

                factor_values = {
                    "CHoCHConfirmation": 1.0,
                    "FreshOrderBlock": 1.0 if ob is not None else 0.0,
                    "FVGAlignment": 1.0 if fvg is not None else 0.0,
                    "TrendAlignment": 1.0,   # by construction: HTF and LTF direction always agree here
                }
                confidence, contributions = compute_confidence(factor_values)
                if confidence < config.confidence_threshold:
                    continue

                seq += 1
                condition_codes = ["TrendContinuation", "BullishCHoCH" if direction == "bullish" else "BearishCHoCH"]
                if ob is not None:
                    condition_codes.append("BullishOrderBlock" if direction == "bullish" else "BearishOrderBlock")
                if fvg is not None:
                    condition_codes.append("FVGAligned")
                reason_codes = build_reason_codes("S6", condition_codes, confidence)

                signal = Signal(
                    signal_id=make_signal_id("S6", context.symbol, "M1", t, seq),
                    strategy_id="S6",
                    timestamp=t,
                    symbol=context.symbol,
                    timeframe="M1",
                    direction=direction,
                    entry_zone=(entry_low, entry_high),
                    stop_loss_reference=stop_ref,
                    target_reference=entry_high + (entry_high - entry_low) if direction == "bullish" else entry_low - (entry_high - entry_low),
                    confidence_score=confidence,
                    reason_codes=reason_codes,
                    confluence_snapshot={
                        "order_block_id": ob["ob_id"] if ob is not None else None,
                        "fvg_id": fvg["fvg_id"] if fvg is not None else None,
                        "liquidity_id": None,
                        "choch_timestamp": m1_choch["break_candle_timestamp"],
                        "swept_timestamp": None,
                    },
                    market_structure_state=context.structure_state_asof(config.bias_timeframe, t),
                    session=next((s for s in context.session_config.windows.keys() if context.session_active_asof(s, t)), None),
                    risk_reference={"type": "location_extreme", "value": stop_ref},
                    metadata={"confidence_contributions": contributions, "bias_event_timestamp": str(after_ts)},
                )
                signals.append(signal)
                break  # only the first qualifying M1 candle per HTF trigger event

    # Overlapping/adjacent same-direction HTF trigger events can
    # independently rediscover the SAME M1 entry candle -- dedupe_signals
    # (shared by every strategy in this codebase) collapses those to one.
    return dedupe_signals(signals)
