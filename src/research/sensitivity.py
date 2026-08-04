"""
Sensitivity Analysis: generates a parameter "response curve" -- how
expectancy, profit factor, and trade count change as a single parameter
is varied across a range of candidate values (e.g. the task brief's
confidence-threshold example: 50, 55, 60, ..., 80).

This is exactly `src.research.parameter_sweep`'s one-parameter case,
wrapped with a name that matches how a researcher actually wants to use
it (a single response curve to look at / plot), rather than a new
sweeping mechanism -- there is deliberately only one implementation of
"vary one parameter and measure the outcome" in this codebase.
"""

from __future__ import annotations

import pandas as pd

from src.research.parameter_sweep import grid_sweep


def parameter_response_curve(symbol, m1, context, configs: dict, target_key: str, field: str, values: list, strategy_filter=None, starting_balance: float = 10_000.0) -> pd.DataFrame:
    """Returns one row per candidate value with num_trades/win_rate/
    profit_factor/expectancy/net_profit/max_drawdown_pct -- the full
    response curve for that single parameter."""
    df = grid_sweep(symbol, m1, context, configs, [(target_key, field, values)], strategy_filter=strategy_filter, starting_balance=starting_balance)
    return df.sort_values(f"{target_key}.{field}").reset_index(drop=True)


def find_best_value(response_curve: pd.DataFrame, param_column: str, metric: str = "expectancy") -> dict:
    if response_curve.empty:
        return {}
    best = response_curve.loc[response_curve[metric].idxmax()]
    return {"value": best[param_column], metric: best[metric], "num_trades": best["num_trades"]}


def detect_diminishing_returns(response_curve: pd.DataFrame, param_column: str, metric: str = "expectancy", tolerance: float = 0.01) -> dict:
    """A simple, deterministic read on whether pushing the parameter
    further keeps helping: compares the metric's value at the last two
    points on the curve. Not a statistical test -- a cheap sanity signal
    for "has this stopped improving" that a researcher can look at
    before deciding whether to extend the sweep range."""
    if len(response_curve) < 2:
        return {"has_data": False}
    sorted_curve = response_curve.sort_values(param_column)
    last_two = sorted_curve[metric].iloc[-2:].to_numpy()
    delta = last_two[1] - last_two[0]
    return {
        "has_data": True,
        "still_improving": bool(delta > tolerance * abs(last_two[0]) if last_two[0] != 0 else delta > tolerance),
        "last_delta": round(float(delta), 4),
    }
