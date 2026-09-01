"""
Task 12 Phase 1/3 -- Parameterized Sunday/Monday gap-reversion signal
generator, for the Research Robustness Layer ONLY.

This is a deliberately SEPARATE module from `src.strategies.s1_monday_gap`
-- it does not import from, modify, or share config with S1. Per this
task's explicit constraint ("do not modify S1/S3/S4/IOS/ITQS/Decision
Engine"), production strategy code is completely untouched; this module
exists purely so the video-inspired research methodology (turn every
discretionary concept into a tested parameter, per
docs/VIDEO_25K_ICT_RESEARCH_METHODOLOGY.md) can be applied to the gap-
reversion hypothesis without risking any change to what the live platform
actually trades.

Every detection primitive is reused UNCHANGED from `src.strategies.context.
MarketContext` (weekend gaps, CHoCH, Order Blocks, FVGs, sessions) --
nothing here re-implements feature detection; it only composes existing,
already-tested primitives into signals under a much richer, explicitly
named parameter set than any single production strategy exposes.

No look-ahead: identical discipline to every other strategy module in
this codebase -- every condition is evaluated using only context data
timestamped <= the candle under evaluation (`MarketContext`'s "asof"
helpers already enforce this by construction).
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional

import numpy as np
import pandas as pd

from src.strategies.common import Signal, compute_confidence, build_reason_codes, make_signal_id


@dataclass(frozen=True)
class GapResearchConfig:
    """Every field here is a "dial" from the video's parameterization
    philosophy (methodology doc Sec. 22) -- nothing is a fixed assumption."""

    # --- Gap definition ---
    gap_min_pct: float = 0.10          # minimum |gap_pct| to consider (percent of Friday close)
    gap_max_pct: Optional[float] = None  # None = no cap
    gap_direction: str = "both"        # "up" | "down" | "both" -- which reopen direction to trade the reversion of

    # --- Structural confirmation ---
    require_choch: bool = True
    choch_timeframe: str = "M5"
    entry_choch_timeframe: str = "M1"

    # --- Order Block ---
    require_ob: bool = True
    ob_timeframe: str = "M15"
    ob_min_quality: float = 0.0        # quality_score threshold, 0-1 (Task 9's validated OB metric)

    # --- FVG (Phase 3 parameterization) ---
    require_fvg: bool = False
    fvg_timeframe: str = "M15"
    fvg_min_size_pct: float = 0.0      # minimum FVG size as % of entry price
    fvg_max_size_pct: Optional[float] = None
    fvg_retracement_pct_required: float = 0.0   # minimum filled_percentage of the FVG required before entry (0 = none)
    fvg_must_be_unfilled: bool = False  # if True, filled_percentage must be exactly 0 at signal time
    fvg_max_age_candles: Optional[int] = None

    # --- Entry / stop / target (method names match the EXISTING
    # EntryConfig/StopLossConfig/TakeProfitConfig registries in
    # src.backtest -- no new execution logic is introduced, only new
    # ways to REACH those existing methods) ---
    stop_reference: str = "ob_extreme"     # StopLossConfig.method
    target_style: str = "gap_fill_50"      # TakeProfitConfig.method
    risk_reward: float = 2.0               # used only when target_style == "fixed_rr"

    # --- Filters ---
    session_filter: Optional[tuple] = None
    day_of_week_filter: Optional[tuple] = None  # pandas dayofweek ints, e.g. (0,) = Monday only
    volatility_filter_atr_mult: Optional[float] = None  # require current True Range >= this x recent-average TR
    confidence_threshold: float = 0.0

    def param_dict(self) -> dict:
        return asdict(self)


def _atr_at(m1: pd.DataFrame, idx: int, period: int = 14) -> tuple:
    """Returns (current_true_range, recent_average_true_range) using only
    candles up to and including idx -- no look-ahead."""
    lo = max(0, idx - period)
    window = m1.iloc[lo:idx + 1]
    if len(window) < 2:
        return 0.0, 0.0
    prev_close = window["close"].shift(1)
    tr = pd.concat([
        window["high"] - window["low"],
        (window["high"] - prev_close).abs(),
        (window["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    tr = tr.dropna()
    if tr.empty:
        return 0.0, 0.0
    return float(tr.iloc[-1]), float(tr.mean())


def generate_gap_reversion_signals(context, config: GapResearchConfig) -> list:
    """Mirrors S1's sequencing discipline (see
    src/strategies/s1_monday_gap.py's docstring for the pattern this is
    deliberately modeled on) but with every threshold exposed as a
    GapResearchConfig field. Only the FIRST qualifying M1 candle per gap
    produces a signal."""
    signals = []
    seq = 0
    m1 = context.m1
    gaps = context.weekend_gaps
    if gaps.empty:
        return signals

    for _, gap in gaps.iterrows():
        if abs(gap["gap_pct"]) < config.gap_min_pct:
            continue
        if config.gap_max_pct is not None and abs(gap["gap_pct"]) > config.gap_max_pct:
            continue
        if config.gap_direction != "both" and gap["gap_direction"] != config.gap_direction:
            continue

        reversal_direction = "bearish" if gap["gap_direction"] == "up" else "bullish"
        reopen_ts = gap["reopen_timestamp"]

        window = m1[m1["timestamp"] > reopen_ts]
        for candle in window.itertuples(index=False):
            t = candle.timestamp
            idx = candle.Index if hasattr(candle, "Index") else None

            if config.day_of_week_filter is not None and t.dayofweek not in config.day_of_week_filter:
                continue
            if config.session_filter and not any(context.session_active_asof(s, t) for s in config.session_filter):
                continue

            if config.require_choch:
                choch = context.latest_choch_asof(config.entry_choch_timeframe, t, direction=reversal_direction)
                if choch is None or choch["break_candle_timestamp"] < reopen_ts:
                    continue
                choch_ts = choch["break_candle_timestamp"]
            else:
                choch_ts = None

            ob = context.fresh_order_block_asof(config.ob_timeframe, reversal_direction, t) if config.require_ob or config.ob_min_quality > 0 else None
            if config.require_ob:
                if ob is None:
                    continue
                if ob.get("quality_score") is not None and ob["quality_score"] < config.ob_min_quality:
                    continue

            fvg = None
            if config.require_fvg or config.fvg_min_size_pct > 0 or config.fvg_must_be_unfilled:
                fvg = context.active_fvg_asof(config.fvg_timeframe, reversal_direction, t)
                if config.require_fvg and fvg is None:
                    continue
                if fvg is not None:
                    fvg_size_pct = (fvg["size"] / candle.close * 100.0) if candle.close else 0.0
                    if fvg_size_pct < config.fvg_min_size_pct:
                        continue
                    if config.fvg_max_size_pct is not None and fvg_size_pct > config.fvg_max_size_pct:
                        continue
                    if fvg.get("filled_percentage", 0.0) < config.fvg_retracement_pct_required:
                        continue
                    if config.fvg_must_be_unfilled and fvg.get("filled_percentage", 0.0) > 0.0:
                        continue
                    if config.fvg_max_age_candles is not None and fvg.get("age", 0) > config.fvg_max_age_candles:
                        continue

            if config.volatility_filter_atr_mult is not None:
                pos = m1.index[m1["timestamp"] == t]
                if len(pos) == 0:
                    continue
                current_tr, avg_tr = _atr_at(m1, int(pos[0]))
                if avg_tr <= 0 or current_tr < config.volatility_filter_atr_mult * avg_tr:
                    continue

            entry_low, entry_high = (ob["low"], ob["high"]) if ob is not None else (
                (fvg["bottom"], fvg["top"]) if fvg is not None else (candle.low, candle.high)
            )

            factor_values = {
                "GapQuality": min(abs(gap["gap_pct"]) / max(config.gap_min_pct * 3, 1e-9), 1.0),
                "CHoCHConfirmation": 1.0 if choch_ts is not None else 0.0,
                "FreshOrderBlock": 1.0 if ob is not None else 0.0,
                "FVGAlignment": 1.0 if fvg is not None else 0.0,
            }
            confidence, contributions = compute_confidence(factor_values)
            if confidence < config.confidence_threshold:
                continue

            seq += 1
            condition_codes = ["GapReversion", "BullishReversal" if reversal_direction == "bullish" else "BearishReversal"]
            if choch_ts is not None:
                condition_codes.append("CHoCHConfirmed")
            if ob is not None:
                condition_codes.append("FreshOrderBlock")
            if fvg is not None:
                condition_codes.append("FVGAligned")
            reason_codes = build_reason_codes("GAPR", condition_codes, confidence)

            signal = Signal(
                signal_id=make_signal_id("GAPR", context.symbol, "M1", t, seq),
                strategy_id="GAPR",
                timestamp=t,
                symbol=context.symbol,
                timeframe="M1",
                direction=reversal_direction,
                entry_zone=(entry_low, entry_high),
                stop_loss_reference=entry_low if reversal_direction == "bullish" else entry_high,
                target_reference=gap["friday_close"],
                confidence_score=confidence,
                reason_codes=reason_codes,
                confluence_snapshot={
                    "order_block_id": ob["ob_id"] if ob is not None else None,
                    "fvg_id": fvg["fvg_id"] if fvg is not None else None,
                    "liquidity_id": None,
                    "choch_timestamp": choch_ts,
                    "swept_timestamp": None,
                },
                market_structure_state=context.structure_state_asof(config.choch_timeframe, t),
                session=next((s for s in (context.session_config.windows.keys() if hasattr(context, "session_config") else []) if context.session_active_asof(s, t)), None),
                risk_reference={"type": config.stop_reference, "value": entry_low if reversal_direction == "bullish" else entry_high},
                metadata={
                    "friday_close": float(gap["friday_close"]), "reopen_open": float(gap["reopen_open"]),
                    "gap_pct": float(gap["gap_pct"]), "gap_direction": gap["gap_direction"], "gap_id": gap["gap_id"],
                    "confidence_contributions": contributions,
                },
            )
            signals.append(signal)
            break  # only the first qualifying candle per gap

    return signals
