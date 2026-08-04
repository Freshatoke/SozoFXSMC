"""
Task 10 Phase 5 — Institutional Risk Layer.

Account-level checks, evaluated ONCE per decision cycle (not per
opportunity) -- these protect the account as a whole, distinct from
Phase 3's per-opportunity concentration/capacity checks. Limits mirror
`config.settings.RiskConfig` (Task 4) so the decision engine's account
protection is consistent with the backtest engine's own `RiskTracker`,
not a competing, disconnected set of numbers.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AccountState:
    """The decision engine's view of account state at the moment a
    decision cycle runs -- supplied by the caller (paper trading module
    in Phase 8, or a live account feed in a future task)."""
    starting_balance: float = 10_000.0
    balance: float = 10_000.0
    daily_pnl: float = 0.0
    weekly_pnl: float = 0.0
    monthly_pnl: float = 0.0
    open_positions_risk_pct: float = 0.0   # sum of allocated risk % already committed
    session_exposure: dict = None          # {"london": 2, "tokyo": 1, ...} open positions per session


@dataclass
class RiskLimits:
    max_daily_loss_pct: float = 0.03    # matches RiskConfig.max_daily_loss_pct default
    max_weekly_loss_pct: float = 0.06   # matches RiskConfig.max_weekly_loss_pct default
    max_monthly_loss_pct: float = 0.10  # institutional convention, one level above weekly -- not in RiskConfig, added here since Task 10 explicitly asks for a monthly check
    max_portfolio_heat_pct: float = 3.0  # matches AllocationLimits.max_portfolio_risk_pct -- kept as its own constant since this module must not import portfolio_allocation (risk layer is account-level, allocation is opportunity-level; duplicated on purpose to keep the two concerns independently testable)
    max_session_exposure: int = 3        # at most 3 concurrent positions opened during any one session


DEFAULT_RISK_LIMITS = RiskLimits()


def evaluate_account_risk(account: AccountState, limits: RiskLimits = DEFAULT_RISK_LIMITS) -> tuple:
    """Returns (allowed: bool, reasons: list[str]) -- if allowed is False,
    NO new trades should be approved this cycle regardless of opportunity
    quality (existing open positions are unaffected; this only gates NEW
    entries, exactly like RiskTracker.can_open in the backtest engine)."""
    reasons = []
    if -account.daily_pnl >= limits.max_daily_loss_pct * account.starting_balance:
        reasons.append(f"Daily loss limit reached ({limits.max_daily_loss_pct*100:.1f}% of starting balance)")
    if -account.weekly_pnl >= limits.max_weekly_loss_pct * account.starting_balance:
        reasons.append(f"Weekly loss limit reached ({limits.max_weekly_loss_pct*100:.1f}% of starting balance)")
    if -account.monthly_pnl >= limits.max_monthly_loss_pct * account.starting_balance:
        reasons.append(f"Monthly loss limit reached ({limits.max_monthly_loss_pct*100:.1f}% of starting balance)")
    if account.open_positions_risk_pct >= limits.max_portfolio_heat_pct:
        reasons.append(f"Portfolio heat at or above limit ({limits.max_portfolio_heat_pct}%)")
    return (len(reasons) == 0), reasons


def check_session_exposure(session: str, account: AccountState, limits: RiskLimits = DEFAULT_RISK_LIMITS) -> tuple:
    exposure = (account.session_exposure or {}).get(session, 0)
    if exposure >= limits.max_session_exposure:
        return False, f"Session {session} already at max concurrent exposure ({limits.max_session_exposure})"
    return True, None
