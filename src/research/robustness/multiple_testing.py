"""
Task 12 Phase 5 -- Multiple-testing correction.

Directly implements the quantitative argument made in
docs/VIDEO_METHODOLOGY_STATISTICAL_AUDIT.md Sec. 1: testing N
configurations and picking the best-looking one(s) inflates the false-
discovery rate unless corrected for. This module implements exactly the
three methods that document names as the minimum bar, plus documents
(not silently assumes) each one's own limitations.

ASSUMPTIONS AND LIMITATIONS (read before trusting any output here):

- The raw per-configuration significance test is a one-sample t-test on
  the per-trade P&L (or R-multiple) sequence, H0: mean <= 0. This
  assumes trade P&Ls are approximately independent and identically
  distributed within a configuration -- a simplification. Trades from
  the same strategy on overlapping gaps are not perfectly independent
  (shared market conditions), so the raw p-values here are OPTIMISTIC
  (too easy to reach significance) even before any multiple-testing
  correction is applied. This is a known, named limitation, not hidden.
- Bonferroni assumes independence ACROSS configurations too, which is
  even less true (configurations that differ by one parameter produce
  highly correlated trade sets) -- Bonferroni is therefore CONSERVATIVE
  here (over-corrects), not exact. It is included because it is simple,
  standard, and gives a defensible worst-case bound, not because it is
  the statistically ideal tool for this exact problem.
- Benjamini-Hochberg (FDR) is less conservative than Bonferroni and is
  the more commonly recommended choice for large, correlated test
  batteries in quantitative finance -- but it controls the EXPECTED
  proportion of false discoveries among rejections, not the probability
  of any false discovery at all (a different, weaker guarantee than
  Bonferroni's family-wise error control).
- Neither method knows anything about OUT-OF-SAMPLE performance. A
  configuration can pass BH-FDR correction on in-sample/validation data
  and still fail out-of-sample -- these are separate, complementary
  checks (see search_engine.py's period="out_of_sample" runs).
"""

from __future__ import annotations

from dataclasses import dataclass
from math import erf, sqrt

import numpy as np


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def one_sample_t_test_pvalue(values: list) -> tuple:
    """One-sided t-test, H0: mean(values) <= 0. Returns (t_statistic, p_value).
    Uses a normal approximation for the p-value (valid for the trade
    counts this framework's own minimum-sample-size gate requires, n>=30
    -- see robustness_score.py) rather than pulling in scipy for a single
    function, keeping this module dependency-free."""
    arr = np.array(values, dtype=float)
    n = len(arr)
    if n < 2:
        return 0.0, 1.0
    mean = arr.mean()
    std = arr.std(ddof=1)
    if std == 0:
        return (float("inf") if mean > 0 else 0.0), (0.0 if mean > 0 else 1.0)
    t_stat = mean / (std / sqrt(n))
    p_value = 1.0 - _norm_cdf(t_stat)   # one-sided: P(T > t_stat) under H0
    return round(float(t_stat), 4), round(float(max(p_value, 0.0)), 8)


def bonferroni_correction(p_values: list, alpha: float = 0.05) -> dict:
    """Family-wise error rate control. Returns per-index reject/fail
    plus the corrected alpha threshold actually used."""
    n = len(p_values)
    if n == 0:
        return {"corrected_alpha": alpha, "reject": []}
    corrected_alpha = alpha / n
    reject = [p < corrected_alpha for p in p_values]
    return {"corrected_alpha": corrected_alpha, "reject": reject, "n_tests": n, "n_rejected": sum(reject)}


def benjamini_hochberg_correction(p_values: list, alpha: float = 0.05) -> dict:
    """False Discovery Rate control (Benjamini-Hochberg 1995 step-up
    procedure). Returns per-original-index reject flags plus each
    p-value's BH-adjusted q-value."""
    n = len(p_values)
    if n == 0:
        return {"reject": [], "q_values": []}
    indexed = sorted(range(n), key=lambda i: p_values[i])
    sorted_p = [p_values[i] for i in indexed]

    # BH critical values: p_(k) <= (k/n) * alpha, find largest k satisfying this
    thresholds = [(k + 1) / n * alpha for k in range(n)]
    below = [sorted_p[k] <= thresholds[k] for k in range(n)]
    if any(below):
        k_max = max(k for k, b in enumerate(below) if b)
    else:
        k_max = -1

    reject_sorted = [i <= k_max for i in range(n)]
    # q-value: smallest FDR at which this p-value would be rejected (monotone, non-decreasing from the largest p-value down)
    q_sorted = [0.0] * n
    running_min = 1.0
    for k in range(n - 1, -1, -1):
        candidate = sorted_p[k] * n / (k + 1)
        running_min = min(running_min, candidate)
        q_sorted[k] = round(min(running_min, 1.0), 8)

    reject = [False] * n
    q_values = [0.0] * n
    for rank, orig_idx in enumerate(indexed):
        reject[orig_idx] = reject_sorted[rank]
        q_values[orig_idx] = q_sorted[rank]

    return {"reject": reject, "q_values": q_values, "n_tests": n, "n_rejected": sum(reject), "alpha": alpha}


@dataclass
class DeflatedSharpeResult:
    observed_sharpe: float
    n_trials: int
    expected_max_sharpe_under_null: float
    deflated_sharpe_ratio: float   # probability the TRUE Sharpe exceeds 0, after deflating for n_trials
    note: str


def deflated_sharpe_ratio(observed_sharpe: float, n_trials: int, n_observations: int, skew: float = 0.0, kurtosis: float = 3.0) -> DeflatedSharpeResult:
    """Approximates Bailey & Lopez de Prado's Deflated Sharpe Ratio: asks
    "given that we picked the best of `n_trials` correlated strategies,
    what Sharpe ratio would we EXPECT to see from the best one by chance
    alone (assuming zero true skill), and how much does the observed
    Sharpe exceed that expectation?"

    LIMITATION: this is a simplified approximation (assumes trial Sharpe
    ratios are approximately i.i.d. standard normal under the null, via
    the expected-maximum-of-n-Gaussians formula) -- NOT the full
    closed-form estimator from the original paper, which also corrects
    for the higher moments (skew/kurtosis) of the actual return
    distribution and for cross-trial correlation. Treat this as a
    directionally useful, honestly-labeled approximation, not a
    publication-grade statistic."""
    if n_trials < 1 or n_observations < 2:
        return DeflatedSharpeResult(observed_sharpe, n_trials, 0.0, 0.5, "insufficient trials/observations for a meaningful estimate")

    # Expected maximum of n_trials standard normal draws (classic extreme-value approximation).
    euler_mascheroni = 0.5772156649
    if n_trials == 1:
        expected_max_z = 0.0
    else:
        expected_max_z = (1 - euler_mascheroni) * _inv_norm_cdf(1 - 1 / n_trials) + euler_mascheroni * _inv_norm_cdf(1 - 1 / (n_trials * np.e))
    expected_max_sharpe = expected_max_z / sqrt(n_observations)

    sharpe_std = sqrt((1 - skew * observed_sharpe + (kurtosis - 1) / 4 * observed_sharpe ** 2) / (n_observations - 1)) if n_observations > 1 else 1.0
    if sharpe_std <= 0:
        dsr = 0.5
    else:
        z = (observed_sharpe - expected_max_sharpe) / sharpe_std
        dsr = round(_norm_cdf(z), 4)

    return DeflatedSharpeResult(
        observed_sharpe=round(observed_sharpe, 4), n_trials=n_trials,
        expected_max_sharpe_under_null=round(expected_max_sharpe, 4), deflated_sharpe_ratio=dsr,
        note="Simplified approximation of Bailey et al.'s DSR -- see this module's docstring for exact limitations.",
    )


def _inv_norm_cdf(p: float) -> float:
    """Acklam's rational approximation to the inverse standard normal CDF
    -- adequate precision for this module's purposes (a few significant
    figures), avoids adding scipy as a dependency for one function."""
    if p <= 0.0:
        return -8.0
    if p >= 1.0:
        return 8.0
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02, 1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02, 6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00, -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00, 3.754408661907416e+00]
    p_low, p_high = 0.02425, 1 - 0.02425
    if p < p_low:
        q = sqrt(-2 * np.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p <= p_high:
        q = p - 0.5
        r = q * q
        return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
    q = sqrt(-2 * np.log(1 - p))
    return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
