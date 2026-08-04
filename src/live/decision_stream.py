"""
Task 11 Phase 5 — Live Decision Engine.

Wraps Task 10's `select_trades` (Phase 4 of that task -- IOS ranking +
portfolio allocation + account risk gate, already tested in
tests/test_decision_engine.py) so it runs CONTINUOUSLY across many
decision cycles instead of Task 10's original single "one day's worth of
opportunities decided together" call. Every cycle:

    new opportunities (from strategy_runner.py) + still-POSTPONEd ones
      -> select_trades(..., open_portfolio=<positions still open from
         earlier cycles>)   <- Task 11 Phase 5's `open_portfolio` addition
      -> EXECUTE   -> added to the live open portfolio, exposure updated
         POSTPONE  -> re-queued for the next cycle (capacity may free up)
         IGNORE    -> logged and dropped (this candidate itself is dead)

Maintains exactly what the task brief asks for: Opportunity Queue
(postponed_queue), Approved trades (open_portfolio / approved_log),
Rejected trades (rejected_log), Portfolio heat / currency exposure /
strategy exposure (properties below, reusing Task 10's own
`portfolio_allocation.currency_exposure`/`strategy_exposure` so the
live view can never disagree with the allocation checks that produced
it), Risk limits (self.risk_limits, unchanged from Task 10).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from src.decision_engine.risk_layer import AccountState, RiskLimits, DEFAULT_RISK_LIMITS
from src.decision_engine.portfolio_allocation import (
    AllocationLimits, DEFAULT_ALLOCATION_LIMITS, currency_exposure, strategy_exposure,
)
from src.decision_engine.trade_selection import select_trades, Decision


@dataclass
class LiveDecisionEngine:
    account: AccountState = field(default_factory=AccountState)
    strategy_correlation: object = None
    allocation_limits: AllocationLimits = field(default_factory=lambda: DEFAULT_ALLOCATION_LIMITS)
    risk_limits: RiskLimits = field(default_factory=lambda: DEFAULT_RISK_LIMITS)

    open_portfolio: list = field(default_factory=list)     # Opportunity objects, EXECUTEd and still open
    postponed_queue: list = field(default_factory=list)    # Opportunity objects awaiting capacity
    approved_log: list = field(default_factory=list)       # Decision objects, all-time
    rejected_log: list = field(default_factory=list)       # Decision objects (IGNOREd), all-time
    cycles_run: int = 0
    on_event: object = None    # Task 11 Phase 7: (event_type: str, detail: dict) -> None, for the audit trail

    def _emit(self, event_type: str, **detail) -> None:
        if self.on_event is not None:
            self.on_event(event_type, detail)

    def on_new_opportunities(self, new_opportunities: list) -> list:
        """One decision cycle. Returns this cycle's list of `Decision`
        objects (for the caller -- e.g. Phase 6's broker to open EXECUTEd
        trades, Phase 7's event logger to record every verdict)."""
        candidates = self.postponed_queue + list(new_opportunities)
        self.postponed_queue = []
        if not candidates:
            self.cycles_run += 1
            return []

        decisions = select_trades(
            candidates, self.account, self.strategy_correlation,
            self.allocation_limits, self.risk_limits, open_portfolio=self.open_portfolio,
        )
        for d in decisions:
            self._emit("ios_calculated", opportunity_id=d.opportunity.opportunity_id, symbol=d.opportunity.symbol,
                        strategy_id=d.opportunity.strategy_id, ios=d.ios, ios_tier=d.ios_tier)
            if d.verdict == "EXECUTE":
                self.open_portfolio.append(d.opportunity)
                self.approved_log.append(d)
                self._emit("trade_approved", opportunity_id=d.opportunity.opportunity_id, symbol=d.opportunity.symbol,
                            ios=d.ios, allocated_risk_pct=d.allocated_risk_pct)
            elif d.verdict == "POSTPONE":
                self.postponed_queue.append(d.opportunity)
                self._emit("trade_postponed", opportunity_id=d.opportunity.opportunity_id, reasons=d.reasons_against)
            else:
                self.rejected_log.append(d)
                self._emit("trade_rejected", opportunity_id=d.opportunity.opportunity_id, reasons=d.reasons_against)

        self.cycles_run += 1
        return decisions

    def close_position(self, opportunity_id: str, realized_pnl: float) -> Optional[Decision]:
        """Called by Phase 6's paper broker when an open position exits.
        Removes it from the live portfolio and updates account state so
        the NEXT decision cycle's risk/exposure checks reflect reality --
        without this, closed trades would keep consuming portfolio heat
        and currency-exposure capacity forever."""
        match = next((o for o in self.open_portfolio if o.opportunity_id == opportunity_id), None)
        if match is None:
            return None
        self.open_portfolio.remove(match)

        risk_pct = getattr(match, "_allocated_risk_pct", 0.0)
        self.account.open_positions_risk_pct = max(0.0, self.account.open_positions_risk_pct - risk_pct)
        self.account.balance += realized_pnl
        self.account.daily_pnl += realized_pnl
        self.account.weekly_pnl += realized_pnl
        self.account.monthly_pnl += realized_pnl
        if match.session and self.account.session_exposure:
            self.account.session_exposure[match.session] = max(0, self.account.session_exposure.get(match.session, 1) - 1)
        return match

    # ------------------------------------------------------------------
    # Continuous state, for Phase 8's monitoring dashboard
    # ------------------------------------------------------------------
    @property
    def portfolio_heat_pct(self) -> float:
        return round(sum(getattr(o, "_allocated_risk_pct", 0.0) for o in self.open_portfolio), 4)

    @property
    def currency_exposure(self) -> dict:
        return currency_exposure(self.open_portfolio)

    @property
    def strategy_exposure(self) -> dict:
        return strategy_exposure(self.open_portfolio)
