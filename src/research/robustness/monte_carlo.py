"""
Task 12 Phase 4 -- Monte Carlo trade-sequence robustness.

Two DISTINCT, clearly-labeled methods (per this task's explicit
instruction to "clearly distinguish bootstrap resampling / trade-order
randomization / any other method"):

  TRADE_ORDER_RANDOMIZATION -- shuffles the SAME set of trade P&Ls into a
      different chronological order. Tests whether the observed
      drawdown/return path depended on a lucky ORDERING of real trades,
      not on which trades occurred.

  BOOTSTRAP_RESAMPLING -- draws len(trades) P&Ls WITH REPLACEMENT from
      the observed trade set. Tests the sampling distribution of
      expectancy/return itself (some trades appear 0, 2, 3+ times), a
      different and complementary question from pure reordering.

Both operate ONLY on the sequence of already-closed, already-simulated
trade P&Ls (or R-multiples) -- this module does not re-run the backtest
engine, re-detect features, or touch look-ahead-sensitive logic in any
way; it is pure post-hoc statistics over a fixed set of numbers.

IMPORTANT LIMITATION (documented per this task's explicit instruction):
neither method proves anything about FUTURE profitability. Both only
answer "how much of the observed equity curve's shape is attributable to
the specific realized order/composition of these historical trades,
versus the underlying per-trade edge itself?" A strategy can pass every
check here and still have zero real edge if its per-trade expectancy is
a backtest artifact (see multiple_testing.py for that separate question).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class MonteCarloResult:
    method: str                 # "TRADE_ORDER_RANDOMIZATION" | "BOOTSTRAP_RESAMPLING"
    n_simulations: int
    seed: int
    n_trades: int
    observed_total_return: float
    observed_max_drawdown: float
    total_return_distribution: dict     # {"mean", "std", "p5", "p50", "p95"}
    expectancy_distribution: dict
    max_drawdown_distribution: dict
    profit_factor_distribution: dict
    losing_streak_distribution: dict
    probability_of_negative_return: float
    probability_of_exceeding_observed_drawdown: float
    risk_of_ruin_pct: float             # fraction of simulations where equity <= 0 at some point


def _equity_path(pnls: list, starting_balance: float) -> np.ndarray:
    return starting_balance + np.cumsum(pnls)


def _max_drawdown(equity: np.ndarray) -> float:
    running_max = np.maximum.accumulate(equity)
    dd = (equity - running_max)
    return float(dd.min())  # negative or 0


def _longest_losing_streak(pnls: np.ndarray) -> int:
    longest = current = 0
    for p in pnls:
        if p <= 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _profit_factor(pnls: np.ndarray) -> float:
    gross_profit = pnls[pnls > 0].sum()
    gross_loss = abs(pnls[pnls <= 0].sum())
    if gross_loss == 0:
        return float("inf") if gross_profit > 0 else 0.0
    return float(gross_profit / gross_loss)


def _summarize(values: np.ndarray) -> dict:
    return {
        "mean": round(float(np.mean(values)), 4), "std": round(float(np.std(values, ddof=1)), 4) if len(values) > 1 else 0.0,
        "p5": round(float(np.percentile(values, 5)), 4), "p50": round(float(np.percentile(values, 50)), 4),
        "p95": round(float(np.percentile(values, 95)), 4),
    }


def _run_simulations(pnls: list, method: str, n_simulations: int, seed: int, starting_balance: float) -> MonteCarloResult:
    if not pnls:
        raise ValueError("Monte Carlo requires at least 1 trade P&L; got an empty sequence.")

    pnls_arr = np.array(pnls, dtype=float)
    n = len(pnls_arr)
    rng = np.random.default_rng(seed)

    observed_equity = _equity_path(pnls_arr, starting_balance)
    observed_return = float(observed_equity[-1] - starting_balance)
    observed_dd = _max_drawdown(observed_equity)

    total_returns, expectancies, max_dds, profit_factors, streaks, ruin_count, exceed_dd_count = [], [], [], [], [], 0, 0

    for _ in range(n_simulations):
        if method == "TRADE_ORDER_RANDOMIZATION":
            sim_pnls = rng.permutation(pnls_arr)
        elif method == "BOOTSTRAP_RESAMPLING":
            sim_pnls = rng.choice(pnls_arr, size=n, replace=True)
        else:
            raise ValueError(f"Unknown Monte Carlo method: {method}")

        equity = _equity_path(sim_pnls, starting_balance)
        dd = _max_drawdown(equity)
        total_returns.append(float(equity[-1] - starting_balance))
        expectancies.append(float(sim_pnls.mean()))
        max_dds.append(dd)
        profit_factors.append(_profit_factor(sim_pnls))
        streaks.append(_longest_losing_streak(sim_pnls))
        if equity.min() <= 0:
            ruin_count += 1
        if dd <= observed_dd:  # more negative = worse drawdown
            exceed_dd_count += 1

    total_returns = np.array(total_returns)
    return MonteCarloResult(
        method=method, n_simulations=n_simulations, seed=seed, n_trades=n,
        observed_total_return=round(observed_return, 4), observed_max_drawdown=round(observed_dd, 4),
        total_return_distribution=_summarize(total_returns),
        expectancy_distribution=_summarize(np.array(expectancies)),
        max_drawdown_distribution=_summarize(np.array(max_dds)),
        profit_factor_distribution=_summarize(np.array([p for p in profit_factors if np.isfinite(p)]) if any(np.isfinite(profit_factors)) else np.array([0.0])),
        losing_streak_distribution=_summarize(np.array(streaks)),
        probability_of_negative_return=round(float((total_returns < 0).mean()), 4),
        probability_of_exceeding_observed_drawdown=round(exceed_dd_count / n_simulations, 4),
        risk_of_ruin_pct=round(100.0 * ruin_count / n_simulations, 4),
    )


def run_trade_order_randomization(pnls: list, n_simulations: int = 5_000, seed: int = 0, starting_balance: float = 10_000.0) -> MonteCarloResult:
    return _run_simulations(pnls, "TRADE_ORDER_RANDOMIZATION", n_simulations, seed, starting_balance)


def run_bootstrap_resampling(pnls: list, n_simulations: int = 5_000, seed: int = 0, starting_balance: float = 10_000.0) -> MonteCarloResult:
    return _run_simulations(pnls, "BOOTSTRAP_RESAMPLING", n_simulations, seed, starting_balance)
