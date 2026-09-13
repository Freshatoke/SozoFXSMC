"""
Task 12.2 -- tests for the live/batch parity harness itself.

These test the HARNESS's own correctness (ground-truth extraction,
row-identity comparison, point-in-time verification), not S3/S4's
behavior (already covered elsewhere) and not a specific parity result
(documented, with real data, in docs/TASK_12_2_LIVE_CONTEXT_PARITY_REPORT.md).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.research.robustness.live_batch_parity import (
    build_batch_ground_truth, build_live_context_at, live_signals_at,
    run_parity_check, verify_point_in_time_correctness, _row_id,
)


def _synthetic_m1(n=3000, seed=0):
    rng = np.random.default_rng(seed)
    ts = pd.date_range("2023-01-02", periods=n, freq="1min", tz="UTC")
    price = 1.1000 + np.cumsum(rng.normal(0, 0.00005, n))
    high = price + np.abs(rng.normal(0, 0.00003, n))
    low = price - np.abs(rng.normal(0, 0.00003, n))
    open_ = price + rng.normal(0, 0.00002, n)
    return pd.DataFrame({"timestamp": ts, "open": open_, "high": high, "low": low, "close": price})


def test_build_batch_ground_truth_returns_rows_with_required_fields():
    m1 = _synthetic_m1()
    rows = build_batch_ground_truth("EURUSD", m1)
    for r in rows:
        for field in ("strategy_id", "symbol", "direction", "timestamp", "entry_low", "entry_high"):
            assert field in r


def test_verify_point_in_time_correctness_clean_on_real_ground_truth():
    m1 = _synthetic_m1()
    rows = build_batch_ground_truth("EURUSD", m1)
    violations = verify_point_in_time_correctness(rows)
    assert violations == []


def test_verify_point_in_time_correctness_flags_a_future_reference():
    row = {
        "strategy_id": "S3", "timestamp": pd.Timestamp("2023-01-01T00:00:00Z"),
        "choch_timestamp": pd.Timestamp("2023-01-01T01:00:00Z"),  # AFTER the signal's own timestamp
        "swept_timestamp": None,
    }
    violations = verify_point_in_time_correctness([row])
    assert len(violations) == 1


def test_build_live_context_at_only_ingests_the_requested_window():
    m1 = _synthetic_m1()
    t = m1["timestamp"].iloc[1000]
    lookback = pd.Timedelta(hours=2)
    ctx = build_live_context_at("EURUSD", m1, t, lookback)
    ingested = ctx.m1
    assert ingested["timestamp"].max() == t
    assert ingested["timestamp"].min() > t - lookback - pd.Timedelta(minutes=1)


def test_row_id_matches_on_identical_signals_and_differs_on_direction():
    a = {"strategy_id": "S3", "symbol": "EURUSD", "direction": "bullish", "timestamp": pd.Timestamp("2023-01-01")}
    b = dict(a)
    c = dict(a, direction="bearish")
    assert _row_id(a) == _row_id(b)
    assert _row_id(a) != _row_id(c)


def test_run_parity_check_reports_zero_recall_when_live_finds_nothing():
    """A degenerate but useful sanity check: a near-zero lookback (1
    minute) cannot possibly reproduce any multi-candle batch setup, so
    recall must be 0 and every batch signal must be classified as missed
    (never silently dropped or miscounted)."""
    m1 = _synthetic_m1()
    gt = build_batch_ground_truth("EURUSD", m1)
    if not gt:
        return  # nothing to check against in this particular random draw
    result = run_parity_check("EURUSD", m1, gt, "1min_degenerate", pd.Timedelta(minutes=1), sample=3)
    assert result.n_evaluated == min(3, len(gt))
    assert result.n_matched + result.n_missed == result.n_evaluated
    assert result.recall == 0.0
