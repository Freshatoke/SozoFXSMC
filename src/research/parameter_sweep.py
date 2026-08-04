"""
Parameter Sweep Engine.

A "parameter" here is identified by `(target_key, field)`:
    target_key in {"S1".."S5", "entry_config", "stop_config", "tp_config",
                    "execution_config", "risk_config", "management_config"}
    field       a field name on that config's dataclass

This lets a sweep touch ANY of the parameters named in the task brief --
strategy-level filters (min gap size, confidence threshold, OB freshness,
FVG/engulfing/liquidity requirements, CHoCH/BOS timeframe) live on the
S1..S5 config dataclasses; execution-level choices (target style, stop
style, risk:reward, max trade duration, breakeven/trailing rules) live on
the backtest config dataclasses -- both are swept through the exact same
mechanism, with no special-casing per parameter.

Two sweep strategies are provided, per the task brief's explicit
instruction to not rely on brute force alone:

- `grid_sweep`: full cartesian product of every parameter's candidate
  values (still useful, and exact, for a small number of parameters).
- `coordinate_sweep`: a greedy, one-parameter-at-a-time search. For each
  parameter (in the given order), it holds every other parameter at its
  CURRENT best-known value and searches only that one parameter's
  candidate values, then keeps the best value found before moving to the
  next parameter. This is O(sum of each parameter's candidate count)
  instead of grid_sweep's O(product of all candidate counts) -- for 6
  parameters with 5 values each, that's 30 experiments instead of 15,625,
  while still capturing each parameter's main effect and letting later
  parameters adapt to earlier choices (a simple, deterministic,
  non-ML "smarter than brute force" search, not full joint optimisation).
"""

from __future__ import annotations

from dataclasses import replace
from itertools import product

import pandas as pd

from src.research.experiment import run_experiment

BACKTEST_KEYS = {"entry_config", "stop_config", "tp_config", "execution_config", "risk_config", "management_config"}
STRATEGY_KEYS = {"S1", "S2", "S3", "S4", "S5"}


def _split_bundle(configs: dict) -> tuple:
    strategy_configs = {k: v for k, v in configs.items() if k in STRATEGY_KEYS}
    backtest_kwargs = {k: v for k, v in configs.items() if k in BACKTEST_KEYS}
    return strategy_configs, backtest_kwargs


def _apply_override(configs: dict, target_key: str, field: str, value) -> dict:
    updated = dict(configs)
    updated[target_key] = replace(configs[target_key], **{field: value})
    return updated


def _run_one(symbol, m1, context, configs, name, parameter_set, strategy_filter, starting_balance):
    strategy_configs, backtest_kwargs = _split_bundle(configs)
    exp = run_experiment(
        name, symbol, m1, strategy_configs=strategy_configs, strategy_filter=strategy_filter,
        backtest_kwargs=backtest_kwargs, starting_balance=starting_balance,
        parameter_set=parameter_set, context=context,
    )
    m = exp.results["metrics"]
    return {
        "research_id": exp.research_id, **parameter_set,
        "num_trades": exp.results["num_trades"], "win_rate": m["win_rate"],
        "profit_factor": m["profit_factor"], "expectancy": m["expectancy"],
        "net_profit": m["net_profit"], "max_drawdown_pct": m["max_drawdown_pct"],
    }


def grid_sweep(symbol, m1, context, configs: dict, param_specs: list, strategy_filter=None, starting_balance=10_000.0) -> pd.DataFrame:
    """param_specs: [(target_key, field, [values...]), ...]. Full cartesian
    product -- use for a small number of parameters/values."""
    names = [f"{t}.{f}" for t, f, _ in param_specs]
    value_lists = [values for _, _, values in param_specs]
    rows = []
    for combo in product(*value_lists):
        updated = dict(configs)
        for (target_key, field, _), value in zip(param_specs, combo):
            updated = _apply_override(updated, target_key, field, value)
        parameter_set = dict(zip(names, combo))
        rows.append(_run_one(symbol, m1, context, updated, f"grid_{'_'.join(names)}", parameter_set, strategy_filter, starting_balance))
    return pd.DataFrame(rows)


def coordinate_sweep(symbol, m1, context, configs: dict, param_specs: list, strategy_filter=None, starting_balance=10_000.0, metric: str = "expectancy") -> dict:
    """Greedy one-parameter-at-a-time search (see module docstring).
    Returns {"best_configs": dict, "history": pd.DataFrame} where history
    has one row per experiment run (every candidate value tried for every
    parameter, in order), with a `chosen` column marking the winner at
    each step."""
    current = dict(configs)
    history_rows = []

    for target_key, field, values in param_specs:
        candidate_rows = []
        for value in values:
            updated = _apply_override(current, target_key, field, value)
            parameter_set = {f"{target_key}.{field}": value}
            row = _run_one(symbol, m1, context, updated, f"coord_{target_key}_{field}", parameter_set, strategy_filter, starting_balance)
            row["target_key"], row["field"], row["value"] = target_key, field, value
            candidate_rows.append(row)

        best_row = max(candidate_rows, key=lambda r: r[metric])
        for row in candidate_rows:
            row["chosen"] = row is best_row
        history_rows.extend(candidate_rows)

        current = _apply_override(current, target_key, field, best_row["value"])

    return {"best_configs": current, "history": pd.DataFrame(history_rows)}
