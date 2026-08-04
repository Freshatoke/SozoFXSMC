"""
Task 11 Phase 4 — Live Strategy Engine.

Per completed candle: update context (already done by LiveMarketContext.
ingest_m1_candle in Phase 3), run S3 + S4 UNCHANGED against that context,
compute ITQS (reusing src.research.itqs, Task 9's exact formula via the
existing src.decision_engine.opportunity._signal_to_opportunity helper),
and hand back only the OPPORTUNITIES THAT ARE NEW since the last call.

Known, documented limitation (no incremental rewrite of S3/S4 attempted --
out of scope for this task): `generate_signals()` was written for batch
backtesting and re-scans its full swept-liquidity/CHoCH history on every
call rather than only the newest candle. Task 11 Phase 1 fixed genuine
correctness bugs in these modules but intentionally left them structurally
batch-oriented -- rewriting S3/S4 as incremental generators would risk
diverging from the already-validated (Tasks 7-9) backtested behavior.
The mitigation here is deduplication by signal_id: every candle re-runs
the (bounded-by-m1_history_len, not "years of history") scan but only
NEW signal_ids are emitted as opportunities, so "no look-ahead" is exact
(context is always asof the current candle) while "no recomputation" is
approximated (downstream Opportunity/ITQS work is never repeated for an
already-seen signal). This tradeoff is called out explicitly in the
Production Readiness Report (Task 11 Phase 12).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from config.settings import DEFAULT_S3_CONFIG, DEFAULT_S4_CONFIG
from src.strategies import s3_liquidity_sweep, s4_pdh_pdl_sweep
from src.research.trade_features import FeatureContextIndex
from src.decision_engine.opportunity import Opportunity, _signal_to_opportunity


@dataclass
class LiveStrategyRunner:
    """Wraps one symbol's `LiveMarketContext` and runs every enabled
    strategy against it after each new candle, returning only
    newly-produced `Opportunity` objects (never re-emitting one already
    handed to the caller)."""

    context: object  # src.live.context_stream.LiveMarketContext
    s3_config: object = DEFAULT_S3_CONFIG
    s4_config: object = DEFAULT_S4_CONFIG
    strategy_timeframe: str = "M15"
    _seen_signal_ids: set = field(default_factory=set)
    total_signals_seen: int = 0
    total_opportunities_emitted: int = 0

    def on_candle_closed(self) -> list:
        """Call once per completed M1 candle, AFTER
        `context.ingest_m1_candle(...)` has already updated the context.
        Returns a list of brand-new `Opportunity` objects (possibly empty)."""
        all_signals = []
        if self.s3_config.enabled:
            all_signals.extend(s3_liquidity_sweep.generate_signals(self.context, self.s3_config))
        if self.s4_config.enabled:
            all_signals.extend(s4_pdh_pdl_sweep.generate_signals(self.context, self.s4_config))

        new_signals = [s for s in all_signals if s.signal_id not in self._seen_signal_ids]
        if not new_signals:
            return []

        for s in new_signals:
            self._seen_signal_ids.add(s.signal_id)
        self.total_signals_seen += len(new_signals)

        # Rebuilt each call -- bounded by the CURRENT active OB/liquidity/FVG
        # counts (not full history), so this is cheap relative to the
        # generate_signals() scan above, not a "years of history" rebuild.
        index = FeatureContextIndex(self.context, timeframe=self.strategy_timeframe)
        opportunities = [_signal_to_opportunity(s, index) for s in new_signals]
        self.total_opportunities_emitted += len(opportunities)
        return opportunities
