"""
Portfolio Testing: combine any subset of strategies' trades into one
portfolio, compute its equity curve/drawdown, measure correlation between
strategies' daily returns, and build the strategy-comparison table the
task brief asks for (best win rate / expectancy / profit factor / lowest
drawdown / best session / best symbol / best confidence range / best
risk:reward).

This module makes no allocation or capital-scaling decisions of its own
-- "any combination of strategies" is just "filter the trade list to the
strategy_ids you want and re-run the same metrics," which is exactly
what `combine_trades` + `src.backtest.metrics.compute_performance_metrics`
already do together.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.backtest.metrics import build_equity_curve, max_drawdown, compute_performance_metrics, _closed


def combine_trades(trades_by_strategy: dict) -> list:
    """trades_by_strategy: {strategy_id: [Trade, ...]}. Pass any subset
    (one strategy = "individual", several = "any combination", all five
    = "all strategies combined")."""
    combined = []
    for trades in trades_by_strategy.values():
        combined.extend(trades)
    return combined


def portfolio_equity_curve(trades: list, starting_balance: float) -> pd.DataFrame:
    return build_equity_curve(trades, starting_balance)


def portfolio_drawdown(trades: list, starting_balance: float) -> dict:
    curve = build_equity_curve(trades, starting_balance)
    return max_drawdown(curve)


def _daily_pnl_series(trades: list) -> pd.Series:
    closed = _closed(trades)
    if not closed:
        return pd.Series(dtype=float)
    daily = {}
    for t in closed:
        day = t.exit_timestamp.date()
        daily[day] = daily.get(day, 0.0) + t.realized_pnl
    return pd.Series(daily).sort_index()


def strategy_correlation(trades_by_strategy: dict) -> pd.DataFrame:
    """Pearson correlation of each strategy's DAILY realized PnL series
    (reindexed to their shared date range, missing days treated as 0
    PnL -- a day with no closed trade contributes no return)."""
    series = {sid: _daily_pnl_series(trades) for sid, trades in trades_by_strategy.items()}
    all_days = sorted(set().union(*[s.index for s in series.values() if not s.empty])) if any(not s.empty for s in series.values()) else []
    aligned = pd.DataFrame({sid: s.reindex(all_days, fill_value=0.0) for sid, s in series.items()})
    if aligned.empty or len(aligned) < 2:
        return pd.DataFrame(index=list(trades_by_strategy.keys()), columns=list(trades_by_strategy.keys()), dtype=float)
    return aligned.corr()


def compare_strategies(trades_by_strategy: dict, starting_balance: float) -> pd.DataFrame:
    rows = []
    for strategy_id, trades in trades_by_strategy.items():
        metrics = compute_performance_metrics(trades, starting_balance)
        closed = _closed(trades)

        best_session = max(metrics["expectancy_by_session"].items(), key=lambda kv: kv[1]["expectancy"], default=(None, {}))
        best_symbol = max(metrics["expectancy_by_symbol"].items(), key=lambda kv: kv[1]["expectancy"], default=(None, {}))
        best_confidence = max(metrics["expectancy_by_confidence"].items(), key=lambda kv: kv[1]["expectancy"], default=(None, {}))
        winning_r_multiples = [t.r_multiple for t in closed if t.r_multiple is not None and t.r_multiple > 0]
        avg_rr = float(np.mean(winning_r_multiples)) if winning_r_multiples else 0.0

        rows.append({
            "strategy_id": strategy_id,
            "num_trades": len(closed),
            "win_rate": metrics["win_rate"],
            "expectancy": metrics["expectancy"],
            "profit_factor": metrics["profit_factor"],
            "max_drawdown_pct": metrics["max_drawdown_pct"],
            "net_profit": metrics["net_profit"],
            "best_session": best_session[0],
            "best_symbol": best_symbol[0],
            "best_confidence_range": best_confidence[0],
            "avg_winning_risk_reward": round(avg_rr, 4),
        })
    df = pd.DataFrame(rows)
    return df


def summarize_best(comparison: pd.DataFrame) -> dict:
    if comparison.empty:
        return {}
    return {
        "best_win_rate": comparison.loc[comparison.win_rate.idxmax(), "strategy_id"],
        "best_expectancy": comparison.loc[comparison.expectancy.idxmax(), "strategy_id"],
        "best_profit_factor": comparison.loc[comparison.profit_factor.idxmax(), "strategy_id"],
        "lowest_drawdown": comparison.loc[comparison.max_drawdown_pct.idxmax(), "strategy_id"],  # least negative
        "best_risk_reward": comparison.loc[comparison.avg_winning_risk_reward.idxmax(), "strategy_id"],
    }
