"""
Portfolio Research: automatically evaluates every single-strategy,
strategy-pair, three-strategy, and the all-five-combined portfolio, and
reports whether combining strategies actually improves risk-adjusted
performance (return, drawdown, volatility, diversification benefit) --
not just raw return.

Builds directly on `src.backtest.portfolio` (combine_trades,
strategy_correlation) rather than duplicating that logic.
"""

from __future__ import annotations

from itertools import combinations

import numpy as np
import pandas as pd

from src.backtest.portfolio import combine_trades, strategy_correlation, portfolio_equity_curve, portfolio_drawdown
from src.backtest.portfolio import _daily_pnl_series
from src.backtest.metrics import compute_performance_metrics


def generate_combinations(strategy_ids: list) -> list:
    """Every single strategy, every pair, every triple, and the full set
    (skipped as a duplicate if there are fewer than 4 strategies)."""
    combos = []
    for size in (1, 2, 3):
        if size <= len(strategy_ids):
            combos.extend(combinations(sorted(strategy_ids), size))
    if len(strategy_ids) > 3:
        combos.append(tuple(sorted(strategy_ids)))
    # de-duplicate while preserving order (the "all" combo may already be a triple/full set)
    seen, unique = set(), []
    for c in combos:
        if c not in seen:
            seen.add(c)
            unique.append(c)
    return unique


def _volatility(trades: list) -> float:
    series = _daily_pnl_series(trades)
    return float(series.std(ddof=1)) if len(series) > 1 else 0.0


def analyze_portfolio_combinations(trades_by_strategy: dict, starting_balance: float = 10_000.0) -> pd.DataFrame:
    strategy_ids = list(trades_by_strategy.keys())
    individual_vol = {sid: _volatility(trades_by_strategy[sid]) for sid in strategy_ids}

    rows = []
    for combo in generate_combinations(strategy_ids):
        subset = {sid: trades_by_strategy[sid] for sid in combo}
        combined = combine_trades(subset)
        metrics = compute_performance_metrics(combined, starting_balance)
        dd = portfolio_drawdown(combined, starting_balance)

        portfolio_vol = _volatility(combined)
        weighted_avg_vol = float(np.mean([individual_vol[sid] for sid in combo])) if combo else 0.0
        diversification_benefit = round(1 - portfolio_vol / weighted_avg_vol, 4) if weighted_avg_vol > 0 else 0.0

        rows.append({
            "combination": "+".join(combo),
            "num_strategies": len(combo),
            "num_trades": metrics["signal_utilization"]["closed_trades"],
            "net_profit": metrics["net_profit"],
            "expectancy": metrics["expectancy"],
            "win_rate": metrics["win_rate"],
            "profit_factor": metrics["profit_factor"],
            "max_drawdown_pct": round(dd["max_drawdown_pct"], 4),
            "volatility": round(portfolio_vol, 4),
            "diversification_benefit": diversification_benefit,
        })

    return pd.DataFrame(rows).sort_values("expectancy", ascending=False).reset_index(drop=True)


def best_portfolio(analysis: pd.DataFrame, metric: str = "expectancy") -> dict:
    if analysis.empty:
        return {}
    return analysis.loc[analysis[metric].idxmax()].to_dict()


def portfolio_correlation_summary(trades_by_strategy: dict) -> pd.DataFrame:
    return strategy_correlation(trades_by_strategy)
