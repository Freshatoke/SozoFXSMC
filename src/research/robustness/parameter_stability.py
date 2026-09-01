"""
Task 12 Phase 7 -- Parameter stability testing.

For every promising configuration, perturbs each NUMERIC parameter by
one grid-step in each direction (holding every other parameter fixed)
and re-runs the exact same `run_one_configuration` pipeline used by the
search engine -- no separate simulation logic. Distinguishes:

  ROBUST EDGE     -- performance stays profitable (expectancy > 0) across
                      the neighbor values too, not just the exact point tested.
  FRAGILE OPTIMUM -- performance collapses (goes non-positive, or drops by
                      more than `collapse_threshold_pct`) at a neighboring value.

This directly operationalizes the video's own stated diagnostic
(methodology doc Sec. 25: "does X+1 or X-1 still work?") and reuses
`src.research.sensitivity`'s existing pattern of thought (parameter
response curves) without duplicating it -- this module is specific to
the gap-research parameter space and returns a stability VERDICT per
config, which `sensitivity.py`'s existing functions do not.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from src.research.robustness.search_engine import run_one_configuration

# Grid step size per numeric field this module knows how to perturb --
# matches the granularity used when building the original search space
# (Sec. "Parameter ranges" of docs/VIDEO_RESEARCH_EXPERIMENT_PLAN.md).
NUMERIC_STEP_SIZES = {
    "gap_min_pct": 0.05, "gap_max_pct": 0.10, "ob_min_quality": 0.1,
    "fvg_min_size_pct": 0.02, "fvg_max_size_pct": 0.05, "fvg_retracement_pct_required": 10.0,
    "risk_reward": 0.5, "volatility_filter_atr_mult": 0.25,
}


@dataclass
class StabilityResult:
    config_id: str
    base_expectancy: float
    neighbor_results: list       # [{"field", "direction", "value", "expectancy", "num_trades"}, ...]
    verdict: str                 # "ROBUST_EDGE" | "FRAGILE_OPTIMUM" | "INSUFFICIENT_DATA"
    stable_fraction: float       # fraction of tested neighbors that stayed profitable
    collapsed_fields: list       # which fields, when perturbed, flipped to non-profitable


def test_parameter_stability(context, m1_slice: pd.DataFrame, base_params: dict, symbol: str,
                              collapse_threshold_pct: float = 0.5, min_neighbor_trades: int = 5, **run_kwargs) -> StabilityResult:
    base_result = run_one_configuration(context, m1_slice, base_params, symbol, period="stability_base", **run_kwargs)
    base_expectancy = base_result.expectancy

    neighbor_results = []
    collapsed_fields = []
    tested = 0

    for field_name, step in NUMERIC_STEP_SIZES.items():
        if field_name not in base_params or base_params[field_name] is None:
            continue
        base_value = base_params[field_name]
        if not isinstance(base_value, (int, float)):
            continue

        for direction, delta in (("down", -step), ("up", step)):
            neighbor_value = round(base_value + delta, 6)
            if neighbor_value < 0:
                continue
            neighbor_params = dict(base_params)
            neighbor_params[field_name] = neighbor_value

            result = run_one_configuration(context, m1_slice, neighbor_params, symbol, period="stability_neighbor", **run_kwargs)
            tested += 1
            row = {
                "field": field_name, "direction": direction, "base_value": base_value, "value": neighbor_value,
                "expectancy": result.expectancy, "num_trades": result.num_trades, "error": result.error,
            }
            neighbor_results.append(row)

            if result.num_trades < min_neighbor_trades:
                continue  # too few trades to judge -- not counted as either stable or collapsed
            if base_expectancy > 0 and result.expectancy <= 0:
                collapsed_fields.append(f"{field_name}.{direction}")
            elif base_expectancy > 0 and result.expectancy < base_expectancy * (1 - collapse_threshold_pct):
                collapsed_fields.append(f"{field_name}.{direction}")

    judged = [r for r in neighbor_results if r["num_trades"] >= min_neighbor_trades]
    if not judged:
        verdict = "INSUFFICIENT_DATA"
        stable_fraction = 0.0
    else:
        stable_count = len(judged) - len(collapsed_fields)
        stable_fraction = round(stable_count / len(judged), 4)
        verdict = "ROBUST_EDGE" if (base_expectancy > 0 and stable_fraction >= 0.6) else "FRAGILE_OPTIMUM"

    return StabilityResult(
        config_id=base_result.config_id, base_expectancy=base_expectancy, neighbor_results=neighbor_results,
        verdict=verdict, stable_fraction=stable_fraction, collapsed_fields=collapsed_fields,
    )
