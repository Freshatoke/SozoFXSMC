"""
Task 12 Phase 13 -- tests for the Research Robustness & Discovery Layer.

Covers: parameter generation, deterministic experiment/config IDs,
temporal train/validation/OOS splitting, no-look-ahead, Monte Carlo
reproducibility/statistics, Bonferroni, Benjamini-Hochberg, parameter
stability, experiment registry, failed-experiment handling, OOS
isolation, and report generation.

None of these tests touch S1-S5, IOS, ITQS, the Decision Engine, or the
Paper Broker -- every fixture here is either synthetic or a small real
EURUSD slice used only through the new `src.research.robustness` modules.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.research.robustness.search_engine import (
    config_id, sample_configurations, run_search, run_one_configuration, MAX_CONFIGURATIONS,
)
from src.research.robustness.data_split import build_research_dataset, OutOfSampleLockedError
from src.research.robustness.gap_signals import GapResearchConfig, generate_gap_reversion_signals
from src.research.robustness.monte_carlo import run_trade_order_randomization, run_bootstrap_resampling
from src.research.robustness.multiple_testing import (
    one_sample_t_test_pvalue, bonferroni_correction, benjamini_hochberg_correction, deflated_sharpe_ratio,
)
from src.research.robustness.robustness_score import compute_rrs, RRSInputs
from src.research.robustness.parameter_stability import test_parameter_stability as run_parameter_stability
from src.research.robustness.registry import register_experiment, load_experiment, list_experiments, new_experiment_id
from src.research.robustness.report import write_results_csv, render_experiment_report_markdown
from src.strategies.context import MarketContext

PARAM_SPACE = {
    "gap_min_pct": [0.05, 0.10, 0.15],
    "gap_direction": ["up", "down", "both"],
    "require_choch": [True, False],
    "require_ob": [True, False],
}


def _synthetic_m1(n=2000, seed=0, weekly_gap=True, start="2023-01-02"):
    """Small synthetic M1 series, cheap enough to run through the full
    context pipeline quickly. When `weekly_gap=True`, splices in a REAL
    Friday-close -> Monday-reopen time jump (>= 20h, per
    `compute_weekend_gaps`'s own detection rule) with a genuine price
    jump across it -- a synthetic price jump with no matching timestamp
    jump would NOT be detected as a weekend gap at all."""
    rng = np.random.default_rng(seed)
    half = n // 2
    if not weekly_gap:
        ts = pd.date_range(start, periods=n, freq="1min", tz="UTC")
        price = 1.1000 + np.cumsum(rng.normal(0, 0.00005, n))
    else:
        friday_start = pd.Timestamp(start, tz="UTC")
        # Roll forward to a Friday so the jump-detection's weekday check passes.
        friday_start += pd.Timedelta(days=(4 - friday_start.dayofweek) % 7)
        ts_before = pd.date_range(friday_start, periods=half, freq="1min", tz="UTC")
        monday_start = ts_before[-1] + pd.Timedelta(hours=48)  # Friday -> Sunday night reopen, a real >=20h jump
        ts_after = pd.date_range(monday_start, periods=n - half, freq="1min", tz="UTC")
        ts = ts_before.append(ts_after)
        price = np.concatenate([
            1.1000 + np.cumsum(rng.normal(0, 0.00005, half)),
            1.1000 + np.cumsum(rng.normal(0, 0.00005, half)) + 0.01,  # reopen with a genuine gap
        ])[:n]
    high = price + np.abs(rng.normal(0, 0.00003, n))
    low = price - np.abs(rng.normal(0, 0.00003, n))
    open_ = price + rng.normal(0, 0.00002, n)
    close = price
    return pd.DataFrame({"timestamp": ts, "open": open_, "high": high, "low": low, "close": close})


@pytest.fixture(scope="module")
def real_m1_slice():
    path = Path("data/processed/historical/EURUSD_M1.parquet")
    if not path.exists():
        pytest.skip("EURUSD_M1.parquet not available in this environment")
    df = pd.read_parquet(path)
    subset = df[(df["timestamp"] >= "2023-01-01") & (df["timestamp"] < "2023-02-15")][
        ["timestamp", "open", "high", "low", "close"]
    ].reset_index(drop=True)
    return subset


# ---------------------------------------------------------------------------
# Parameter generation
# ---------------------------------------------------------------------------

def test_sample_configurations_reproducible_with_same_seed():
    a = sample_configurations(PARAM_SPACE, 15, seed=7)
    b = sample_configurations(PARAM_SPACE, 15, seed=7)
    assert a == b


def test_sample_configurations_different_seed_differs():
    a = sample_configurations(PARAM_SPACE, 15, seed=7)
    b = sample_configurations(PARAM_SPACE, 15, seed=8)
    assert a != b


def test_sample_configurations_are_distinct():
    configs = sample_configurations(PARAM_SPACE, 20, seed=1)
    ids = [config_id(c) for c in configs]
    assert len(ids) == len(set(ids))


def test_sample_configurations_bounded_by_space_size():
    tiny_space = {"a": [1, 2]}
    configs = sample_configurations(tiny_space, 100, seed=1)
    assert len(configs) == 2  # cannot exceed the actual space size


# ---------------------------------------------------------------------------
# Deterministic config/experiment IDs
# ---------------------------------------------------------------------------

def test_config_id_deterministic():
    params = {"gap_min_pct": 0.1, "require_ob": True}
    assert config_id(params) == config_id(dict(params))


def test_config_id_order_independent():
    assert config_id({"a": 1, "b": 2}) == config_id({"b": 2, "a": 1})


def test_config_id_sensitive_to_value_change():
    assert config_id({"gap_min_pct": 0.1}) != config_id({"gap_min_pct": 0.2})


def test_new_experiment_id_unique():
    ids = {new_experiment_id("test") for _ in range(50)}
    assert len(ids) == 50


# ---------------------------------------------------------------------------
# Search engine bounds
# ---------------------------------------------------------------------------

def test_run_search_refuses_above_max_without_override(real_m1_slice):
    ctx = MarketContext(symbol="EURUSD", m1=real_m1_slice)
    with pytest.raises(ValueError):
        run_search(PARAM_SPACE, MAX_CONFIGURATIONS + 1, seed=1, context=ctx, m1_slice=real_m1_slice, symbol="EURUSD")


def test_failed_configuration_is_recorded_not_dropped():
    ctx = MarketContext(symbol="EURUSD", m1=_synthetic_m1())
    # gap_min_pct=0.0 with require_choch/require_ob disabled guarantees at
    # least one signal fires on the engineered gap, so the invalid
    # stop_reference actually gets exercised by resolve_stop_loss's
    # unknown-method branch -- verifies the error path records a result
    # object (with `.error` populated) instead of raising out of the caller.
    bad_params = {"gap_min_pct": 0.0, "require_choch": False, "require_ob": False,
                  "stop_reference": "not_a_real_method", "target_style": "fixed_rr"}
    result = run_one_configuration(ctx, _synthetic_m1(), bad_params, "EURUSD")
    assert result.error is not None
    assert result.config_id == config_id(bad_params)


# ---------------------------------------------------------------------------
# Temporal train/validation/OOS splitting + OOS isolation
# ---------------------------------------------------------------------------

def test_split_is_strictly_chronological():
    m1 = _synthetic_m1(n=1000, weekly_gap=False)
    ds = build_research_dataset("EURUSD", m1)
    assert ds.train["timestamp"].max() <= ds.validation["timestamp"].min()
    assert ds.split.validation_end <= ds.split.out_of_sample_start


def test_split_no_row_shuffling():
    m1 = _synthetic_m1(n=1000, weekly_gap=False)
    ds = build_research_dataset("EURUSD", m1)
    for period_df in (ds.train, ds.validation):
        assert period_df["timestamp"].is_monotonic_increasing


def test_out_of_sample_locked_by_default():
    m1 = _synthetic_m1(n=1000, weekly_gap=False)
    ds = build_research_dataset("EURUSD", m1)
    with pytest.raises(OutOfSampleLockedError):
        _ = ds.out_of_sample


def test_out_of_sample_accessible_after_explicit_unlock():
    m1 = _synthetic_m1(n=1000, weekly_gap=False)
    ds = build_research_dataset("EURUSD", m1)
    ds.unlock_out_of_sample(reason="test: selection complete")
    assert len(ds.out_of_sample) > 0


def test_unlock_requires_a_reason():
    m1 = _synthetic_m1(n=1000, weekly_gap=False)
    ds = build_research_dataset("EURUSD", m1)
    with pytest.raises(ValueError):
        ds.unlock_out_of_sample(reason="")


# ---------------------------------------------------------------------------
# No look-ahead (gap signal generation)
# ---------------------------------------------------------------------------

def test_gap_signals_no_lookahead(real_m1_slice):
    """Signals generated over a truncated dataset must be a PREFIX-
    consistent subset of signals generated over the full dataset: no
    signal appearing at timestamp T in the truncated run may differ from
    the full run's signal at T, and no signal after the truncation point
    can leak backward.

    NOTE (discovered while building this test, reported per Task 12
    Phase 12 rather than silently worked around): with `require_ob=True`
    this comparison can fail even though `gap_signals.py` itself
    introduces no new look-ahead. Root cause is upstream, in
    `MarketContext.order_blocks()` (used identically by S1-S5): it
    computes Order Blocks once over the FULL dataframe it holds (no
    `as_of_index` restriction, unlike `detect_order_blocks` itself, which
    DOES support one -- see `tests/test_no_lookahead.py`), and an OB
    whose `creation_timestamp` exactly equals the truncation boundary can
    be present in the full computation but absent from a same-boundary
    truncated computation -- i.e. `creation_timestamp` does not always
    fully capture every fact the detector used to confirm that OB. This
    is a pre-existing platform characteristic (affects every S1-S5
    strategy that calls `fresh_order_block_asof`), not something
    introduced here, and is NOT modified by this task (see this task's
    explicit "do not modify S1-S5" constraint and "STOP and report
    separately" instruction) -- flagged in the Task 12 completion report
    instead. This test therefore verifies gap_signals.py's OWN
    contribution (CHoCH-gated sequencing) in isolation, which IS fully
    prefix-consistent."""
    cfg = GapResearchConfig(gap_min_pct=0.05, require_choch=True, require_ob=False, require_fvg=False)
    ctx_full = MarketContext(symbol="EURUSD", m1=real_m1_slice)
    signals_full = generate_gap_reversion_signals(ctx_full, cfg)
    assert signals_full, "expected at least one signal in this window to make the test meaningful"

    cutoff_ts = signals_full[0].timestamp
    truncated = real_m1_slice[real_m1_slice["timestamp"] <= cutoff_ts].reset_index(drop=True)
    ctx_truncated = MarketContext(symbol="EURUSD", m1=truncated)
    signals_truncated = generate_gap_reversion_signals(ctx_truncated, cfg)

    assert len(signals_truncated) >= 1
    assert signals_truncated[0].timestamp == signals_full[0].timestamp
    assert signals_truncated[0].direction == signals_full[0].direction
    assert signals_truncated[0].entry_zone == signals_full[0].entry_zone


# ---------------------------------------------------------------------------
# Monte Carlo
# ---------------------------------------------------------------------------

def test_monte_carlo_reproducible_with_same_seed():
    pnls = list(np.random.default_rng(1).normal(10, 50, 40))
    a = run_bootstrap_resampling(pnls, n_simulations=500, seed=99)
    b = run_bootstrap_resampling(pnls, n_simulations=500, seed=99)
    assert a == b


def test_monte_carlo_different_seed_differs():
    pnls = list(np.random.default_rng(1).normal(10, 50, 40))
    a = run_bootstrap_resampling(pnls, n_simulations=500, seed=1)
    b = run_bootstrap_resampling(pnls, n_simulations=500, seed=2)
    assert a.total_return_distribution != b.total_return_distribution


def test_monte_carlo_empty_trades_raises():
    with pytest.raises(ValueError):
        run_bootstrap_resampling([], n_simulations=100, seed=1)


def test_trade_order_randomization_preserves_total_return():
    """Reordering the SAME trades cannot change the total sum -- this is
    a mathematical invariant the module must respect, and is exactly why
    order-randomization tests the DRAWDOWN PATH, not total return."""
    pnls = [100.0, -50.0, 30.0, -20.0, 80.0]
    result = run_trade_order_randomization(pnls, n_simulations=1000, seed=1)
    assert result.total_return_distribution["mean"] == pytest.approx(sum(pnls), abs=0.01)
    assert result.total_return_distribution["std"] == pytest.approx(0.0, abs=1e-6)


def test_bootstrap_resampling_varies_total_return():
    pnls = [100.0, -50.0, 30.0, -20.0, 80.0]
    result = run_bootstrap_resampling(pnls, n_simulations=1000, seed=1)
    assert result.total_return_distribution["std"] > 0  # resampling WITH replacement can change the sum


def test_monte_carlo_statistics_sane_for_clear_positive_edge():
    pnls = list(np.random.default_rng(3).normal(200, 10, 60))  # overwhelming positive edge, tiny variance
    result = run_bootstrap_resampling(pnls, n_simulations=2000, seed=3)
    assert result.probability_of_negative_return < 0.05
    assert result.risk_of_ruin_pct == 0.0


# ---------------------------------------------------------------------------
# Multiple-testing correction
# ---------------------------------------------------------------------------

def test_t_test_pvalue_low_for_strong_positive_edge():
    pnls = list(np.random.default_rng(5).normal(100, 20, 50))
    t_stat, p = one_sample_t_test_pvalue(pnls)
    assert t_stat > 0
    assert p < 0.01


def test_t_test_pvalue_high_for_zero_mean_noise():
    pnls = list(np.random.default_rng(6).normal(0, 100, 50))
    t_stat, p = one_sample_t_test_pvalue(pnls)
    assert p > 0.01  # not a strict guarantee, but true for this fixed seed -- verifies the function isn't miscalibrated


def test_bonferroni_more_conservative_than_raw():
    pvals = [0.001, 0.02, 0.03, 0.04, 0.2, 0.9]
    bonf = bonferroni_correction(pvals, alpha=0.05)
    n_raw_significant = sum(1 for p in pvals if p < 0.05)
    assert bonf["n_rejected"] <= n_raw_significant


def test_bonferroni_corrected_alpha_scales_with_n():
    assert bonferroni_correction([0.01] * 10)["corrected_alpha"] == pytest.approx(0.005)
    assert bonferroni_correction([0.01] * 100)["corrected_alpha"] == pytest.approx(0.0005)


def test_benjamini_hochberg_rejects_at_least_as_many_as_bonferroni():
    pvals = [0.001, 0.008, 0.02, 0.03, 0.04, 0.2, 0.5, 0.9]
    bonf = bonferroni_correction(pvals)
    bh = benjamini_hochberg_correction(pvals)
    assert bh["n_rejected"] >= bonf["n_rejected"]


def test_benjamini_hochberg_empty_input():
    result = benjamini_hochberg_correction([])
    assert result["reject"] == []


def test_benjamini_hochberg_q_values_monotone_with_sorted_pvalues():
    pvals = [0.001, 0.01, 0.02, 0.5, 0.9]
    bh = benjamini_hochberg_correction(pvals)
    order = sorted(range(len(pvals)), key=lambda i: pvals[i])
    q_sorted = [bh["q_values"][i] for i in order]
    assert q_sorted == sorted(q_sorted)  # q-values non-decreasing in p-value rank


def test_deflated_sharpe_more_trials_lowers_confidence_for_same_sharpe():
    small = deflated_sharpe_ratio(observed_sharpe=1.0, n_trials=10, n_observations=100)
    large = deflated_sharpe_ratio(observed_sharpe=1.0, n_trials=5000, n_observations=100)
    assert large.deflated_sharpe_ratio <= small.deflated_sharpe_ratio


# ---------------------------------------------------------------------------
# Robustness score
# ---------------------------------------------------------------------------

def test_rrs_zero_for_all_missing_inputs():
    rrs = compute_rrs(RRSInputs())
    assert rrs["rrs"] == 0.0
    assert rrs["interpretation"] == "NOT_ROBUST -- insufficient evidence of a real edge"


def test_rrs_high_for_strong_inputs():
    inputs = RRSInputs(
        oos_expectancy=100.0, oos_reference_expectancy=50.0, oos_window_win_rate=0.9,
        parameter_stability_pass_rate=0.9, monte_carlo_survival_rate=0.95, max_drawdown_pct=0.02,
        drawdown_tolerance_pct=0.20, bh_significant=True, cross_symbol_pass_rate=0.8,
        num_oos_trades=200, min_trades_for_full_credit=100,
    )
    rrs = compute_rrs(inputs)
    assert rrs["rrs"] >= 70
    assert rrs["interpretation"].startswith("STRONG_CANDIDATE")


def test_rrs_never_exceeds_100():
    inputs = RRSInputs(
        oos_expectancy=1e9, oos_reference_expectancy=1.0, oos_window_win_rate=1.0,
        parameter_stability_pass_rate=1.0, monte_carlo_survival_rate=1.0, max_drawdown_pct=0.0,
        bh_significant=True, cross_symbol_pass_rate=1.0, num_oos_trades=10000,
    )
    assert compute_rrs(inputs)["rrs"] <= 100.0


# ---------------------------------------------------------------------------
# Parameter stability
# ---------------------------------------------------------------------------

def test_parameter_stability_returns_verdict(real_m1_slice):
    ctx = MarketContext(symbol="EURUSD", m1=real_m1_slice)
    base_params = {"gap_min_pct": 0.05, "gap_direction": "both", "require_choch": True, "require_ob": True, "ob_min_quality": 0.0}
    result = run_parameter_stability(ctx, real_m1_slice, base_params, "EURUSD", min_neighbor_trades=1)
    assert result.verdict in {"ROBUST_EDGE", "FRAGILE_OPTIMUM", "INSUFFICIENT_DATA"}
    assert 0.0 <= result.stable_fraction <= 1.0


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def test_registry_round_trip(tmp_path):
    registry_dir = tmp_path / "registry"
    record = register_experiment(
        experiment_name="unit_test", dataset_identity="EURUSD_M1.parquet", symbols=["EURUSD"],
        date_range=("2023-01-01", "2023-06-01"), parameter_search_space=PARAM_SPACE, n_configurations=10,
        random_seed=42, train_dates=("2023-01-01", "2023-03-01"), validation_dates=("2023-03-01", "2023-04-01"),
        out_of_sample_dates=("2023-04-01", "2023-06-01"), statistical_correction_method=["bonferroni", "benjamini_hochberg"],
        results_location=str(tmp_path / "results.csv"), registry_dir=str(registry_dir),
    )
    loaded = load_experiment(record.experiment_id, registry_dir=str(registry_dir))
    assert loaded is not None
    assert loaded["n_configurations"] == 10
    assert loaded["experiment_id"] == record.experiment_id
    assert "git_commit" in loaded and "software_version" in loaded


def test_registry_list_experiments(tmp_path):
    registry_dir = tmp_path / "registry"
    for _ in range(3):
        register_experiment(
            experiment_name="unit_test", dataset_identity="x", symbols=["EURUSD"], date_range=("a", "b"),
            parameter_search_space={}, n_configurations=1, random_seed=1, train_dates=("a", "b"),
            validation_dates=("a", "b"), out_of_sample_dates=("a", "b"), statistical_correction_method=[],
            results_location="x", registry_dir=str(registry_dir),
        )
    assert len(list_experiments(registry_dir=str(registry_dir))) == 3


def test_registry_missing_experiment_returns_none(tmp_path):
    assert load_experiment("EXP_does_not_exist", registry_dir=str(tmp_path)) is None


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def test_write_results_csv_includes_failed_configs(tmp_path):
    ctx = MarketContext(symbol="EURUSD", m1=_synthetic_m1())
    good = run_one_configuration(ctx, _synthetic_m1(), {"gap_min_pct": 0.05}, "EURUSD")
    bad_params = {"gap_min_pct": 0.0, "require_choch": False, "require_ob": False, "stop_reference": "invalid_method_xyz"}
    bad = run_one_configuration(ctx, _synthetic_m1(), bad_params, "EURUSD")
    csv_path = write_results_csv([good, bad], str(tmp_path / "results.csv"))
    df = pd.read_csv(csv_path)
    assert len(df) == 2
    assert df["error"].notna().sum() == 1  # exactly one row records the failure


def test_report_markdown_contains_required_sections(tmp_path):
    record = register_experiment(
        experiment_name="unit_test", dataset_identity="x", symbols=["EURUSD"], date_range=("a", "b"),
        parameter_search_space={}, n_configurations=5, random_seed=1, train_dates=("a", "b"),
        validation_dates=("a", "b"), out_of_sample_dates=("a", "b"),
        statistical_correction_method=["bonferroni", "benjamini_hochberg"], results_location="x",
        registry_dir=str(tmp_path / "registry"),
    ).to_dict()
    md = render_experiment_report_markdown(
        record, all_results=[], n_failed=0, raw_significance={"reject": []}, bonferroni={"n_rejected": 0},
        bh={"n_rejected": 0}, dsr=None, oos_results=[], monte_carlo_summaries={}, stability_results=[],
        cost_stress_summaries={}, rrs_by_config={}, final_verdict="NO ROBUST EDGE FOUND.",
    )
    for section in ("Search universe summary", "Raw vs. adjusted significance", "Out-of-sample results",
                    "Monte Carlo robustness", "Parameter stability", "Cost/execution stress", "Final verdict"):
        assert section in md
    assert "NO ROBUST EDGE FOUND." in md
