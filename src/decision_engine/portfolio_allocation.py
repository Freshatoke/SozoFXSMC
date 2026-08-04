"""
Task 10 Phase 3 — Portfolio Allocation Rules.

Institutional-style position and exposure limits applied ON TOP of the
IOS ranking (Phase 2) -- these decide how much of the ranked queue
actually gets taken, not which strategies fire. Every default is either
inherited directly from the platform's existing `RiskConfig` (Task 4) or
a documented, conservative institutional-desk convention (never fit to
this dataset).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AllocationLimits:
    max_simultaneous_trades: int = 5          # portfolio-wide, across all strategies/symbols
    max_trades_per_currency: int = 2           # e.g. at most 2 open positions touching USD
    max_trades_per_strategy: int = 3           # matches RiskConfig.max_simultaneous_positions's spirit, widened slightly for a multi-strategy desk
    max_portfolio_risk_pct: float = 3.0         # sum of allocated risk % across all open positions
    correlation_reject_threshold: float = 0.15  # above Task 8's own measured strategy-pair correlations (all <0.13) counts as "meaningfully correlated" for this platform's evidence base
    ios_tier_risk_pct: dict = field(default_factory=lambda: {"A": 1.0, "B": 0.75, "C": 0.5, "D": 0.25})


DEFAULT_ALLOCATION_LIMITS = AllocationLimits()


def ios_tier(ios: float) -> str:
    if ios >= 75:
        return "A"
    if ios >= 60:
        return "B"
    if ios >= 45:
        return "C"
    return "D"


def suggested_risk_pct(ios: float, limits: AllocationLimits = DEFAULT_ALLOCATION_LIMITS) -> float:
    return limits.ios_tier_risk_pct[ios_tier(ios)]


def currency_exposure(portfolio: list) -> dict:
    """{"EUR": 2, "USD": 3, ...} -- count of open/queued positions touching each currency."""
    counts: dict = {}
    for opp in portfolio:
        for leg in opp.currency_legs():
            if leg:
                counts[leg] = counts.get(leg, 0) + 1
    return counts


def strategy_exposure(portfolio: list) -> dict:
    counts: dict = {}
    for opp in portfolio:
        counts[opp.strategy_id] = counts.get(opp.strategy_id, 0) + 1
    return counts


def is_correlated_with_portfolio(opp, portfolio: list, strategy_correlation, limits: AllocationLimits = DEFAULT_ALLOCATION_LIMITS) -> tuple:
    """Returns (is_correlated: bool, reason: str | None). Two checks,
    both evidence-based:
      1. Shared currency leg (the task brief's own EURUSD/GBPUSD example)
         with the SAME direction -- both are effectively the same USD bet.
      2. Cross-strategy correlation above Task 8's measured baseline
         (strategy_correlation is the actual correlation matrix from
         reports/institutional_research/_cache/aggregate.pkl)."""
    opp_legs = set(opp.currency_legs())
    for held in portfolio:
        if held.direction == opp.direction and set(held.currency_legs()) & opp_legs and held.symbol != opp.symbol:
            return True, f"Shares a currency leg with already-held {held.symbol} ({held.strategy_id}) in the same direction"
        if strategy_correlation is not None and held.strategy_id in strategy_correlation.index and opp.strategy_id in strategy_correlation.columns:
            corr = strategy_correlation.loc[held.strategy_id, opp.strategy_id]
            if abs(corr) > limits.correlation_reject_threshold:
                return True, f"Strategy {opp.strategy_id} correlates {corr:.3f} with already-held {held.strategy_id} (threshold {limits.correlation_reject_threshold})"
    return False, None


def check_allocation_limits(opp, portfolio: list, limits: AllocationLimits = DEFAULT_ALLOCATION_LIMITS) -> tuple:
    """Returns (allowed: bool, reason: str | None) -- pure capacity/
    concentration checks, no correlation (see `is_correlated_with_portfolio`
    for that, kept separate so Phase 4 can report each failure distinctly)."""
    if len(portfolio) >= limits.max_simultaneous_trades:
        return False, f"Portfolio already at max simultaneous trades ({limits.max_simultaneous_trades})"

    strat_counts = strategy_exposure(portfolio)
    if strat_counts.get(opp.strategy_id, 0) >= limits.max_trades_per_strategy:
        return False, f"Strategy {opp.strategy_id} already at max concurrent trades ({limits.max_trades_per_strategy})"

    curr_counts = currency_exposure(portfolio)
    for leg in opp.currency_legs():
        if leg and curr_counts.get(leg, 0) >= limits.max_trades_per_currency:
            return False, f"Currency {leg} already at max exposure ({limits.max_trades_per_currency} positions)"

    current_risk = sum(getattr(h, "_allocated_risk_pct", 0.0) for h in portfolio)
    candidate_risk = suggested_risk_pct(getattr(opp, "_ios", 50.0), limits)
    if current_risk + candidate_risk > limits.max_portfolio_risk_pct:
        return False, f"Adding this trade's {candidate_risk}% risk would exceed max portfolio risk ({limits.max_portfolio_risk_pct}%, currently {current_risk}%)"

    return True, None
