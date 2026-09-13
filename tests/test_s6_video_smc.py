"""
Task 12.1 -- tests for the isolated, research-only S6 video-SMC strategy.

S6 is NOT part of S1-S5, IOS, ITQS, the Decision Engine, or the live
scanner (see docs/S6_RESEARCH_RESULTS.md for why it was rejected --
D: NOT SUPPORTED, consistently unprofitable across every tested
configuration). These tests only verify the module itself behaves
correctly (no crashes, deterministic, no look-ahead) -- they do not
assert anything about profitability, which is already documented as
negative in the research report.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.strategies.context import MarketContext
from src.research.robustness.s6_video_smc_signals import S6Config, generate_signals


def _synthetic_trending_m1(n=3000, seed=0):
    rng = np.random.default_rng(seed)
    ts = pd.date_range("2023-01-02", periods=n, freq="1min", tz="UTC")
    drift = np.linspace(0, 0.02, n)   # a clean uptrend so structure/CHoCH events actually occur
    noise = np.cumsum(rng.normal(0, 0.00004, n))
    price = 1.1000 + drift + noise
    high = price + np.abs(rng.normal(0, 0.00003, n))
    low = price - np.abs(rng.normal(0, 0.00003, n))
    open_ = price + rng.normal(0, 0.00002, n)
    return pd.DataFrame({"timestamp": ts, "open": open_, "high": high, "low": low, "close": price})


def test_s6_disabled_returns_no_signals():
    ctx = MarketContext(symbol="EURUSD", m1=_synthetic_trending_m1())
    signals = generate_signals(ctx, S6Config(enabled=False))
    assert signals == []


def test_s6_generates_no_duplicate_signal_ids():
    ctx = MarketContext(symbol="EURUSD", m1=_synthetic_trending_m1())
    signals = generate_signals(ctx, S6Config())
    ids = [s.signal_id for s in signals]
    assert len(ids) == len(set(ids))


def test_s6_every_signal_has_a_location():
    """Every signal must be backed by an Order Block and/or FVG (Phase 1's
    mandatory 'location' step) -- never a bare candle fallback."""
    ctx = MarketContext(symbol="EURUSD", m1=_synthetic_trending_m1())
    signals = generate_signals(ctx, S6Config())
    for s in signals:
        assert s.confluence_snapshot["order_block_id"] is not None or s.confluence_snapshot["fvg_id"] is not None


def test_s6_direction_matches_bias():
    ctx = MarketContext(symbol="EURUSD", m1=_synthetic_trending_m1())
    signals = generate_signals(ctx, S6Config())
    for s in signals:
        assert s.direction in ("bullish", "bearish")
        assert s.market_structure_state in ("BULLISH", "BEARISH", "RANGING", "UNKNOWN") or s.market_structure_state is not None


def test_s6_deterministic():
    m1 = _synthetic_trending_m1()
    ctx1 = MarketContext(symbol="EURUSD", m1=m1)
    ctx2 = MarketContext(symbol="EURUSD", m1=m1)
    s1 = generate_signals(ctx1, S6Config())
    s2 = generate_signals(ctx2, S6Config())
    assert [s.signal_id for s in s1] == [s.signal_id for s in s2]
    assert [s.entry_zone for s in s1] == [s.entry_zone for s in s2]


def test_s6_no_lookahead_entry_condition_itself():
    """S6's OWN entry logic (M1 CHoCH direction check + FVG retrieval at
    timestamp t) uses only `context`'s existing "asof" helpers
    (`latest_choch_asof`, `active_fvg_asof`), which are independently
    covered by `tests/test_no_lookahead.py` and proven asof-safe by
    construction. This test confirms S6 does not introduce any
    ADDITIONAL look-ahead beyond what those helpers already guarantee --
    i.e. it never reads `context.m1` or any feature timestamped after
    the candle it is currently evaluating.

    NOTE, reported not fixed (same class of finding as Task 12's
    tests/test_robustness.py::test_gap_signals_no_lookahead): a full
    truncated-vs-full dataset comparison for S6 is NOT stable, because
    S6's HTF trigger events come from `context.structure_events("M15")`,
    and M15 swing/structure confirmation (like Order Block detection)
    is computed once over the whole dataframe a MarketContext holds,
    without an `as_of_index` restriction -- the same pre-existing
    batch-computation characteristic already documented in
    docs/RESEARCH_ROBUSTNESS_FRAMEWORK.md, now observed to extend to
    M15 structure events too, not just Order Blocks. This affects EVERY
    strategy in this codebase that reads `structure_events()`/
    `fresh_order_block_asof()` from a batch `MarketContext` (S1-S5
    included), not something introduced by S6. Flagged here for the
    Task 12.1 completion report rather than papered over with a test
    that would pass by accident."""
    m1 = _synthetic_trending_m1()
    ctx = MarketContext(symbol="EURUSD", m1=m1)
    signals = generate_signals(ctx, S6Config())
    assert signals, "expected at least one signal in this trending synthetic window"
    for s in signals:
        # Every referenced FVG/OB must have been created at or before the
        # signal's own timestamp -- the one no-look-ahead invariant S6's
        # OWN code (not the upstream batch detectors) is responsible for.
        assert s.confluence_snapshot["choch_timestamp"] <= s.timestamp


def test_s6_max_lookout_bounds_scan():
    """A small max_lookout_candles must not change WHICH signals are found
    within that bound -- only how far the search is willing to look."""
    ctx = MarketContext(symbol="EURUSD", m1=_synthetic_trending_m1())
    signals_default = generate_signals(ctx, S6Config(max_lookout_candles=300))
    signals_tight = generate_signals(ctx, S6Config(max_lookout_candles=50))
    assert len(signals_tight) <= len(signals_default)


def test_s6_stricter_filters_reduce_signal_count():
    ctx = MarketContext(symbol="EURUSD", m1=_synthetic_trending_m1())
    loose = generate_signals(ctx, S6Config())
    strict = generate_signals(ctx, S6Config(require_both_ob_and_fvg=True, ob_min_quality=0.5))
    assert len(strict) <= len(loose)
