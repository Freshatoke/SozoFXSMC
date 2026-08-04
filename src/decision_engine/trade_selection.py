"""
Task 10 Phase 4/7 — Trade Selection Engine + Explainability.

Given a batch of simultaneous opportunities, decides EXECUTE / IGNORE /
POSTPONE for each one, with a human-readable reason -- combining:
    Phase 2  IOS ranking (higher IOS decided first)
    Phase 3  portfolio allocation (capacity/concentration/correlation)
    Phase 5  account-level risk gate (checked once per cycle)

Verdict semantics (deliberately distinct, per the task brief's explicit
three-way split):
    EXECUTE   -- passes every check; becomes part of the portfolio for
                 the rest of this decision cycle (so later opportunities
                 see it for correlation/exposure purposes).
    POSTPONE  -- rejected ONLY for a CAPACITY reason (max simultaneous
                 trades / per-strategy / per-currency limit) that could
                 free up later in the same session -- a fundamentally
                 good opportunity that simply didn't fit this cycle.
    IGNORE    -- rejected for a QUALITY, CORRELATION, or ACCOUNT-RISK
                 reason that would not be resolved by more capacity
                 becoming available -- this opportunity itself is the
                 problem, not the queue.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.decision_engine.ios import compute_ios
from src.decision_engine.portfolio_allocation import (
    AllocationLimits, DEFAULT_ALLOCATION_LIMITS, check_allocation_limits,
    is_correlated_with_portfolio, ios_tier, suggested_risk_pct,
)
from src.decision_engine.risk_layer import AccountState, RiskLimits, DEFAULT_RISK_LIMITS, evaluate_account_risk, check_session_exposure

_CAPACITY_MARKERS = ("max simultaneous trades", "max concurrent trades", "max exposure")


@dataclass
class Decision:
    opportunity: object          # Opportunity
    ios: float
    ios_tier: str
    ios_components: dict
    verdict: str                 # "EXECUTE" | "IGNORE" | "POSTPONE"
    reasons_for: list = field(default_factory=list)
    reasons_against: list = field(default_factory=list)
    allocated_risk_pct: float = 0.0

    def explanation(self) -> str:
        lines = [f"{'Approved' if self.verdict == 'EXECUTE' else self.verdict.capitalize()} because:"]
        for r in self.reasons_for:
            lines.append(f"  ✓ {r}")
        for r in self.reasons_against:
            lines.append(f"  ✗ {r}")
        return "\n".join(lines)


def _explain_ios_components(components: dict) -> list:
    """Turns numeric IOS components into the ✓-style human explanations
    the task's worked examples show (e.g. "✓ Fresh Order Block")."""
    reasons = []
    if components["ob_freshness"] >= 0.9:
        reasons.append("Fresh Order Block")
    if components["ob_quality"] >= 0.7:
        reasons.append("High Quality Order Block")
    if components["itqs"] >= 0.7:
        reasons.append("High ITQS")
    if components["session_suitability"] >= 0.9:
        reasons.append("Preferred session for this strategy")
    if components["regime_suitability"] >= 0.8:
        reasons.append("Favourable market regime (volatility/trend)")
    if components["symbol_strength"] >= 0.7:
        reasons.append("Historically strong symbol for this strategy")
    if components["portfolio_diversification"] >= 0.9:
        reasons.append("Low correlation with current portfolio")
    if components["expected_expectancy"] >= 0.6:
        reasons.append("Strong expected expectancy (R:R x historical win rate)")
    return reasons


def select_trades(
    opportunities: list,
    account: AccountState,
    strategy_correlation=None,
    allocation_limits: AllocationLimits = DEFAULT_ALLOCATION_LIMITS,
    risk_limits: RiskLimits = DEFAULT_RISK_LIMITS,
    open_portfolio: list | None = None,
) -> list:
    """opportunities: list of Opportunity, in ANY order -- sorted here by
    initial (portfolio-independent) IOS descending before deciding, so
    the best opportunities are always considered first regardless of
    caller order. This matters: portfolio-diversification is the only
    IOS component that depends on decision order, so the initial sort
    uses an empty-portfolio IOS (every other component is order-
    independent) -- a candidate's rank can only be refined downward by
    diversification once decided, never used to skip a better opportunity
    in favour of a worse one. Returns one Decision per opportunity, in
    the order they were decided (highest initial IOS first).

    open_portfolio: opportunities already EXECUTEd and still open from a
    PRIOR cycle (default None == empty, preserving Task 10's original
    single-cycle behaviour). Task 11 Phase 5 needs this: a continuously
    running decision engine must weigh new opportunities against
    positions still open from earlier cycles for correlation/exposure
    purposes, not just what's decided within the current cycle."""
    decisions = []
    portfolio: list = list(open_portfolio) if open_portfolio else []  # opportunities EXECUTEd so far this cycle (seeded with still-open prior positions)

    account_ok, account_reasons = evaluate_account_risk(account, risk_limits)

    ranked_opportunities = sorted(
        opportunities, key=lambda o: compute_ios(o, [])["ios"], reverse=True,
    )

    for opp in ranked_opportunities:
        ios_result = compute_ios(opp, portfolio)
        ios, components = ios_result["ios"], ios_result["components"]
        tier = ios_tier(ios)
        reasons_for = _explain_ios_components(components)
        reasons_against = []

        if not account_ok:
            decisions.append(Decision(
                opportunity=opp, ios=ios, ios_tier=tier, ios_components=components,
                verdict="IGNORE", reasons_for=reasons_for, reasons_against=list(account_reasons),
            ))
            continue

        correlated, corr_reason = is_correlated_with_portfolio(opp, portfolio, strategy_correlation, allocation_limits)
        if correlated:
            reasons_against.append(corr_reason)
            lower_ios_note = "Lower IOS than a competing, correlated setup already selected" if portfolio else corr_reason
            decisions.append(Decision(
                opportunity=opp, ios=ios, ios_tier=tier, ios_components=components,
                verdict="IGNORE", reasons_for=reasons_for, reasons_against=[lower_ios_note],
            ))
            continue

        session_ok, session_reason = check_session_exposure(opp.session, account, risk_limits)
        if not session_ok:
            reasons_against.append(session_reason)
            decisions.append(Decision(
                opportunity=opp, ios=ios, ios_tier=tier, ios_components=components,
                verdict="POSTPONE", reasons_for=reasons_for, reasons_against=reasons_against,
            ))
            continue

        allowed, reason = check_allocation_limits(opp, portfolio, allocation_limits)
        if not allowed:
            verdict = "POSTPONE" if any(m in reason for m in _CAPACITY_MARKERS) else "IGNORE"
            decisions.append(Decision(
                opportunity=opp, ios=ios, ios_tier=tier, ios_components=components,
                verdict=verdict, reasons_for=reasons_for, reasons_against=[reason],
            ))
            continue

        risk_pct = suggested_risk_pct(ios, allocation_limits)
        opp._ios = ios
        opp._allocated_risk_pct = risk_pct
        portfolio.append(opp)
        decisions.append(Decision(
            opportunity=opp, ios=ios, ios_tier=tier, ios_components=components,
            verdict="EXECUTE", reasons_for=reasons_for, reasons_against=[], allocated_risk_pct=risk_pct,
        ))

    return decisions
