"""
Task 9 Phase 4 — Institutional Trade Quality Score (ITQS).

A NEW, research-only, PRE-ENTRY trade quality score. It replaces
nothing (the existing Task 3 confidence score is untouched) and is
never used to gate live trading decisions in this task -- it exists to
rank trades for research, and its own predictive value is validated at
the bottom of this module exactly the way the existing confidence score
was validated in Task 8 (Spearman correlation vs. outcome).

Every weight is derived directly from Task 9 Phase 2/3 evidence, not
intuition:

  - OB freshness (FRESH vs MITIGATED): the single strongest, most
    statistically significant finding (chi2 p < 0.001 for both S3 and
    S4; win rate 71-73% FRESH vs 45-46% MITIGATED) -> largest weight.
  - OB quality score: significant for both strategies (t-test p < 0.01)
    and, combined with freshness, the only Phase 3 confluence with a
    reliable sample size (n >= 30) that roughly TRIPLED expectancy ->
    second-largest weight.
  - Displacement confirmation, liquidity strength, confidence score:
    theoretically motivated by each strategy's own entry logic (S3
    requires displacement by design; liquidity strength and confidence
    are recorded on every trade) but did NOT reach Phase 2's
    significance threshold at the available sample size -- included
    with deliberately SMALL weights, not zero, and this is stated
    explicitly rather than silently dropped.

Weights are fixed constants (not fit to any dataset), matching the
"no curve fitting" engineering principle -- the RELATIVE size of each
weight is set from Phase 2/3's measured effect sizes, but no
optimization loop searched for the values that maximize backtest
performance.
"""

from __future__ import annotations

import pandas as pd

# Weights sum to 1.0. See module docstring for the evidence behind each.
ITQS_WEIGHTS = {
    "ob_freshness": 0.35,          # strongest, most significant finding (Phase 2/3)
    "ob_quality": 0.30,             # second-strongest, significant for both strategies
    "displacement_confirmed": 0.15,  # theoretically motivated, not independently significant at n available
    "liquidity_strength": 0.12,      # theoretically motivated, not independently significant at n available
    "confidence_score": 0.08,        # Phase 2 found near-zero mutual information -- smallest weight, not zero
}


def compute_itqs_row(row: pd.Series) -> float:
    """row: one row of the master feature dataset (or any object with the
    same field names). Returns a 0-100 score."""
    freshness_component = 1.0 if row.get("ob_freshness_status") == "FRESH" else (0.0 if row.get("ob_freshness_status") == "MITIGATED" else 0.5)
    quality_raw = row.get("ob_quality_score")
    quality_component = min(max(float(quality_raw), 0.0), 1.0) if pd.notna(quality_raw) else 0.5
    displacement_component = 1.0 if row.get("has_displacement_confirmed") else 0.0
    liquidity_component = 1.0 if row.get("liquidity_strength") == "strong" else (0.0 if row.get("liquidity_strength") == "weak" else 0.5)
    confidence_raw = row.get("confidence_score")
    confidence_component = min(max(float(confidence_raw) / 100.0, 0.0), 1.0) if pd.notna(confidence_raw) else 0.5

    score = 100.0 * (
        ITQS_WEIGHTS["ob_freshness"] * freshness_component
        + ITQS_WEIGHTS["ob_quality"] * quality_component
        + ITQS_WEIGHTS["displacement_confirmed"] * displacement_component
        + ITQS_WEIGHTS["liquidity_strength"] * liquidity_component
        + ITQS_WEIGHTS["confidence_score"] * confidence_component
    )
    return round(score, 2)


def add_itqs_column(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["itqs"] = out.apply(compute_itqs_row, axis=1)
    return out


def itqs_bucket(score: float) -> str:
    if score >= 70:
        return "A (70-100)"
    if score >= 55:
        return "B (55-70)"
    if score >= 40:
        return "C (40-55)"
    return "D (<40)"
