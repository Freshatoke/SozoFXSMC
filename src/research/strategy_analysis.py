"""
Strategy Analysis: the full per-strategy metric suite named in the task
brief. Every metric except Calmar is already computed by
`src.backtest.metrics.compute_performance_metrics` (Task 4); this module
adds Calmar Ratio (annualized return / |max drawdown|) and assembles the
one-row-per-strategy comparison table.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.backtest.trade import TradeStatus
from src.backtest.metrics import compute_performance_metrics


def calmar_ratio(trades: list, starting_balance: float, annualization_days: float = 365.0) -> float:
    """Annualized return / |max drawdown %|. The annualization simply
    extrapolates the dataset's actual return over its actual date span to
    a full year -- meaningful mainly as a RELATIVE ranking tool across
    strategies tested on the same dataset/period, not as a claim about
    real annual performance (a short backtest window annualizes noisily;
    see docs/RESEARCH_LAB.md's Known Limitations)."""
    closed = [t for t in trades if t.status == TradeStatus.CLOSED.value]
    if not closed:
        return 0.0
    metrics = compute_performance_metrics(trades, starting_balance)
    max_dd_pct = abs(metrics["max_drawdown_pct"])
    if max_dd_pct == 0:
        return float("inf") if metrics["net_profit"] > 0 else 0.0

    timestamps = [t.exit_timestamp for t in closed if t.exit_timestamp is not None]
    span_days = max((max(timestamps) - min(timestamps)).total_seconds() / 86400.0, 1.0) if timestamps else 1.0
    total_return = metrics["net_profit"] / starting_balance
    annualized_return = total_return * (annualization_days / span_days)
    return round(annualized_return / max_dd_pct, 4)


def analyze_strategies(trades_by_strategy: dict, starting_balance: float = 10_000.0) -> pd.DataFrame:
    rows = []
    for strategy_id, trades in trades_by_strategy.items():
        closed = [t for t in trades if t.status == TradeStatus.CLOSED.value]
        metrics = compute_performance_metrics(trades, starting_balance)
        r_dist = metrics["r_multiple_distribution"]
        rows.append({
            "strategy_id": strategy_id,
            "num_trades": len(closed),
            "expectancy": metrics["expectancy"],
            "profit_factor": metrics["profit_factor"],
            "win_rate": metrics["win_rate"],
            "average_winner": metrics["average_winner"],
            "average_loser": metrics["average_loser"],
            "max_drawdown_pct": metrics["max_drawdown_pct"],
            "average_trade_duration_candles": metrics["average_trade_duration_candles"],
            "r_multiple_mean": r_dist["mean"],
            "r_multiple_std": r_dist["std"],
            "average_mae": metrics["average_mae"],
            "average_mfe": metrics["average_mfe"],
            "recovery_factor": metrics["recovery_factor"],
            "sharpe_ratio": metrics["sharpe_ratio"],
            "sortino_ratio": metrics["sortino_ratio"],
            "calmar_ratio": calmar_ratio(trades, starting_balance),
        })
    return pd.DataFrame(rows).sort_values("expectancy", ascending=False).reset_index(drop=True)
