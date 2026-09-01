"""
Task 12 Phase 6 -- Research Robustness Score (RRS).

RESEARCH-ONLY. This score never feeds into, replaces, or is read by
`src.decision_engine.ios` or `src.research.itqs` -- those remain exactly
as validated in Tasks 9/10, unmodified, for pre-trade opportunity ranking
in the live/backtest pipeline. RRS answers a completely different
question ("should this CANDIDATE CONFIGURATION be trusted as a real
edge, having survived a large search?"), asked only during research, not
per-trade.

FORMULA (documented transparently, per this task's explicit instruction
-- every weight below is a stated, arbitrary-but-declared judgment call,
not a fitted/optimized value, since fitting the SCORE ITSELF to the data
would reintroduce exactly the overfitting risk this framework exists to
control for):

    RRS = 100 * (
        0.20 * oos_expectancy_component +
        0.15 * oos_consistency_component +
        0.15 * parameter_stability_component +
        0.15 * monte_carlo_survival_component +
        0.10 * drawdown_component +
        0.10 * significance_component +      # multiple-testing-adjusted
        0.10 * cross_symbol_component +
        0.05 * sample_size_component
    )

Every component is clamped to [0, 1] before weighting. A component that
cannot be computed (e.g. cross-symbol not yet run) is treated as 0, not
skipped/reweighted -- an incomplete robustness check should LOWER the
score, not be silently ignored, per Phase 12's governance rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

RRS_WEIGHTS = {
    "oos_expectancy": 0.20, "oos_consistency": 0.15, "parameter_stability": 0.15,
    "monte_carlo_survival": 0.15, "drawdown": 0.10, "significance": 0.10,
    "cross_symbol": 0.10, "sample_size": 0.05,
}


@dataclass
class RRSInputs:
    oos_expectancy: Optional[float] = None            # currency units per trade, out-of-sample
    oos_reference_expectancy: float = 50.0             # normalization anchor (documented, arbitrary): expectancy at/above this maps to component=1.0
    oos_window_win_rate: Optional[float] = None         # fraction of walk-forward windows with positive expectancy
    parameter_stability_pass_rate: Optional[float] = None  # fraction of neighbor-perturbations that stayed profitable
    monte_carlo_survival_rate: Optional[float] = None    # 1 - probability_of_negative_return (bootstrap)
    max_drawdown_pct: Optional[float] = None             # observed OOS max drawdown, as a fraction (0.10 = 10%)
    drawdown_tolerance_pct: float = 0.20                 # drawdown at/above this maps to component=0.0
    bh_significant: Optional[bool] = None                 # passed Benjamini-Hochberg at alpha=0.05
    cross_symbol_pass_rate: Optional[float] = None        # fraction of tested symbols with positive OOS expectancy
    num_oos_trades: Optional[int] = None
    min_trades_for_full_credit: int = 100                 # trades at/above this maps to sample_size component=1.0


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def compute_rrs(inputs: RRSInputs) -> dict:
    components = {}

    components["oos_expectancy"] = _clamp01(inputs.oos_expectancy / inputs.oos_reference_expectancy) if inputs.oos_expectancy is not None and inputs.oos_reference_expectancy > 0 else 0.0
    components["oos_consistency"] = _clamp01(inputs.oos_window_win_rate) if inputs.oos_window_win_rate is not None else 0.0
    components["parameter_stability"] = _clamp01(inputs.parameter_stability_pass_rate) if inputs.parameter_stability_pass_rate is not None else 0.0
    components["monte_carlo_survival"] = _clamp01(inputs.monte_carlo_survival_rate) if inputs.monte_carlo_survival_rate is not None else 0.0
    components["drawdown"] = _clamp01(1.0 - inputs.max_drawdown_pct / inputs.drawdown_tolerance_pct) if inputs.max_drawdown_pct is not None and inputs.drawdown_tolerance_pct > 0 else 0.0
    components["significance"] = 1.0 if inputs.bh_significant else 0.0
    components["cross_symbol"] = _clamp01(inputs.cross_symbol_pass_rate) if inputs.cross_symbol_pass_rate is not None else 0.0
    components["sample_size"] = _clamp01(inputs.num_oos_trades / inputs.min_trades_for_full_credit) if inputs.num_oos_trades is not None and inputs.min_trades_for_full_credit > 0 else 0.0

    rrs = 100.0 * sum(RRS_WEIGHTS[k] * v for k, v in components.items())

    return {
        "rrs": round(rrs, 2), "components": {k: round(v, 4) for k, v in components.items()},
        "weights": RRS_WEIGHTS, "missing_components": [k for k in components if components[k] == 0.0 and _is_input_missing(inputs, k)],
        "interpretation": _interpret(rrs),
    }


def _is_input_missing(inputs: RRSInputs, component: str) -> bool:
    mapping = {
        "oos_expectancy": inputs.oos_expectancy, "oos_consistency": inputs.oos_window_win_rate,
        "parameter_stability": inputs.parameter_stability_pass_rate, "monte_carlo_survival": inputs.monte_carlo_survival_rate,
        "drawdown": inputs.max_drawdown_pct, "significance": inputs.bh_significant,
        "cross_symbol": inputs.cross_symbol_pass_rate, "sample_size": inputs.num_oos_trades,
    }
    return mapping.get(component) is None


def _interpret(rrs: float) -> str:
    # Thresholds are deliberately conservative and stated plainly -- this
    # is a research triage label, not a claim of validated profitability.
    if rrs >= 70:
        return "STRONG_CANDIDATE -- survives most robustness gates; still requires forward/paper validation before any production consideration"
    if rrs >= 40:
        return "WEAK_CANDIDATE -- partial evidence only; several robustness gates failed or were not computed"
    return "NOT_ROBUST -- insufficient evidence of a real edge"
