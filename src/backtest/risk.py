"""
Risk Management: position sizing methods plus a stateful `RiskTracker`
that gates new trade entries against portfolio-level limits as the
backtest progresses through time.

No Martingale, no Grid: every sizing method here computes a position size
from the CURRENT account balance and the CURRENT stop distance only --
none of them scale up after a loss (that is the defining, explicitly
prohibited behaviour of Martingale/Grid systems), and there is no code
path that reads "past losses" to determine the NEXT trade's size.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from config.settings import RiskConfig, ExecutionConfig


def _round_lots(lots: float, config: RiskConfig) -> float:
    lots = max(config.min_lot_size, min(config.max_lot_size, lots))
    return round(lots / config.min_lot_size) * config.min_lot_size


def size_fixed_lot(balance: float, stop_distance: float, config: RiskConfig, execution_config: ExecutionConfig, atr: float = 0.0) -> float:
    return _round_lots(config.fixed_lot_size, config)


def size_fixed_percentage_risk(balance: float, stop_distance: float, config: RiskConfig, execution_config: ExecutionConfig, atr: float = 0.0) -> float:
    if stop_distance <= 0:
        return config.min_lot_size
    risk_amount = balance * config.risk_per_trade_pct
    # 1 lot moving `stop_distance` in price loses stop_distance * contract_size
    # in account currency, so lots = risk_amount / (stop_distance * contract_size).
    lots = risk_amount / (stop_distance * execution_config.contract_size)
    return _round_lots(lots, config)


def size_fixed_monetary_risk(balance: float, stop_distance: float, config: RiskConfig, execution_config: ExecutionConfig, atr: float = 0.0) -> float:
    if stop_distance <= 0:
        return config.min_lot_size
    lots = config.fixed_monetary_risk / (stop_distance * execution_config.contract_size)
    return _round_lots(lots, config)


def size_volatility_adjusted(balance: float, stop_distance: float, config: RiskConfig, execution_config: ExecutionConfig, atr: float = 0.0) -> float:
    """Same risk-based formula as `fixed_percentage_risk` (position size
    already scales inversely with stop distance, which is itself
    volatility-derived when the stop method is ATR-based), with an
    additional explicit halving when the CURRENT ATR is elevated (>1.5x
    the stop distance implied ATR-equivalent) -- a simple, deterministic
    extra damper for unusually volatile entries."""
    base = size_fixed_percentage_risk(balance, stop_distance, config, execution_config)
    if atr > 0 and stop_distance > 0 and atr > 1.5 * stop_distance:
        base = base / 2
    return _round_lots(base, config)


SIZING_METHODS = {
    "fixed_lot": size_fixed_lot,
    "fixed_percentage_risk": size_fixed_percentage_risk,
    "fixed_monetary_risk": size_fixed_monetary_risk,
    "volatility_adjusted": size_volatility_adjusted,
}


def resolve_position_size(balance: float, stop_distance: float, config: RiskConfig, execution_config: ExecutionConfig, atr: float = 0.0) -> float:
    method = SIZING_METHODS.get(config.sizing_method)
    if method is None:
        raise ValueError(f"Unknown position sizing method: {config.sizing_method}")
    return method(balance, stop_distance, config, execution_config, atr)


@dataclass
class RiskTracker:
    """Stateful, time-ordered gate for new entries. Call `update_on_close`
    after every trade closes (in chronological order of closure) and
    `can_open` before every candidate entry.

    Task 11 Phase 1 fix: the consecutive-loss gate is a CIRCUIT BREAKER
    (pauses new entries for `config.consecutive_loss_cooldown_days` after
    `max_consecutive_losses` is hit), not a permanent lockout. The
    original implementation only reset `consecutive_losses` on a WINNING
    trade's close -- but once the gate was tripped, `can_open` rejected
    every candidate, so no trade could ever open to produce the winning
    close that would reset it. That is a genuine deadlock, discovered in
    Task 8 (backtests over a full multi-year history silently rejected
    ~97% of signals after an early losing streak) and worked around there
    with a permissive research-only config rather than fixed at the
    source. This is the proper fix: the cooldown expires on ELAPSED TIME
    (`locked_out_until`), independent of whether any trade closes during
    it, so the circuit breaker always resets -- exactly the "temporary
    pause with a real reset mechanism" Task 8/10 both recommended as
    follow-up work."""

    config: RiskConfig
    balance: float = 0.0
    daily_pnl: dict = field(default_factory=dict)
    weekly_pnl: dict = field(default_factory=dict)
    consecutive_losses: int = 0
    open_positions: int = 0
    open_positions_risk: float = 0.0
    locked_out_until: Optional[pd.Timestamp] = None

    def __post_init__(self):
        if self.balance == 0.0:
            self.balance = self.config.starting_balance

    def _day_key(self, ts: pd.Timestamp):
        return ts.date()

    def _week_key(self, ts: pd.Timestamp):
        return ts.isocalendar()[:2]

    def can_open(self, timestamp: pd.Timestamp, risk_amount: float) -> tuple:
        """Returns (allowed: bool, reason: Optional[str])."""
        if self.open_positions >= self.config.max_simultaneous_positions:
            return False, "MaxSimultaneousPositions"

        day_pnl = self.daily_pnl.get(self._day_key(timestamp), 0.0)
        if -day_pnl >= self.config.max_daily_loss_pct * self.balance:
            return False, "MaxDailyLossReached"

        week_pnl = self.weekly_pnl.get(self._week_key(timestamp), 0.0)
        if -week_pnl >= self.config.max_weekly_loss_pct * self.balance:
            return False, "MaxWeeklyLossReached"

        if self.locked_out_until is not None:
            if timestamp < self.locked_out_until:
                return False, "ConsecutiveLossCooldownActive"
            # Cooldown has elapsed: the circuit breaker resets regardless
            # of whether any trade closed during the pause -- this is
            # what makes it a genuine reset instead of the old deadlock.
            self.locked_out_until = None
            self.consecutive_losses = 0

        if (self.open_positions_risk + risk_amount) > self.config.max_portfolio_exposure_pct * self.balance:
            return False, "MaxPortfolioExposureReached"

        return True, None

    def register_open(self, risk_amount: float) -> None:
        self.open_positions += 1
        self.open_positions_risk += risk_amount

    def register_close(self, timestamp: pd.Timestamp, risk_amount: float, realized_pnl: float) -> None:
        self.open_positions = max(0, self.open_positions - 1)
        self.open_positions_risk = max(0.0, self.open_positions_risk - risk_amount)
        self.balance += realized_pnl

        day_key, week_key = self._day_key(timestamp), self._week_key(timestamp)
        self.daily_pnl[day_key] = self.daily_pnl.get(day_key, 0.0) + realized_pnl
        self.weekly_pnl[week_key] = self.weekly_pnl.get(week_key, 0.0) + realized_pnl

        if realized_pnl < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= self.config.max_consecutive_losses and self.locked_out_until is None:
                self.locked_out_until = timestamp + pd.Timedelta(days=self.config.consecutive_loss_cooldown_days)
        elif realized_pnl > 0:
            self.consecutive_losses = 0
            self.locked_out_until = None
