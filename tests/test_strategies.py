"""
Task 3 Strategy Engine tests.

Two layers:
1. Unit-level tests of pure helper functions (gap checkpoints, MAE/MFE,
   BOS-pair finding, confidence scoring, reason codes, dedup) using small
   hand-crafted inputs -- fully deterministic, no market data needed.
2. Integration-level tests running the full strategy engine over a
   deterministic synthetic multi-day dataset (tests/helpers.make_multi_day_m1),
   asserting schema/invariant properties (every required Signal field is
   present, no duplicates, reproducibility, no look-ahead, configuration
   overrides/suppression work) rather than exact hand-picked values, since
   hand-crafting SMC-aligned data across 5 independent multi-timeframe
   strategies is impractical at unit-test granularity.
"""

import pandas as pd
import pytest

from config.settings import S1Config, S2Config, S3Config
from src.strategies.context import MarketContext
from src.strategies.common import compute_confidence, build_reason_codes, dedupe_signals, Signal
from src.strategies.runner import run_strategies, STRATEGY_MODULES, signals_to_dataframe
from src.strategies.s1_monday_gap import _gap_fill_checkpoints, _mae_mfe
from src.strategies.s2_third_bos import _find_bos_pairs
from tests.helpers import make_multi_day_m1

REQUIRED_SIGNAL_FIELDS = [
    "signal_id", "strategy_id", "timestamp", "symbol", "timeframe", "direction",
    "entry_zone", "stop_loss_reference", "target_reference", "confidence_score",
    "reason_codes", "confluence_snapshot", "market_structure_state", "session",
    "risk_reference", "metadata",
]


@pytest.fixture(scope="module")
def market_context():
    m1 = make_multi_day_m1(num_days=10)
    return MarketContext(symbol="TEST", m1=m1)


@pytest.fixture(scope="module")
def all_signals(market_context):
    return run_strategies(market_context)


# ---------------------------------------------------------------------------
# Unit-level: gap detection / fill tracking
# ---------------------------------------------------------------------------


def test_gap_fill_checkpoints_monotonic_and_bounded():
    m1 = pd.DataFrame({
        "timestamp": pd.date_range("2024-01-08 22:00:00", periods=10, freq="1min", tz="UTC"),
        "open": [1.108] * 10, "high": [1.1085] * 10,
        "low": [1.108 - 0.001 * i for i in range(10)],  # progressively deeper retracement
        "close": [1.108 - 0.001 * i for i in range(10)],
    })
    gap = pd.Series({
        "reopen_timestamp": m1["timestamp"].iloc[0] - pd.Timedelta(minutes=1),
        "reopen_open": 1.108, "friday_close": 1.100,
    })
    checkpoints = _gap_fill_checkpoints(m1, gap, reversal_direction="bearish")
    order = ["25%", "50%", "75%", "100%"]
    timestamps = [checkpoints[k] for k in order if checkpoints[k] is not None]
    assert timestamps == sorted(timestamps)  # thresholds reached in non-decreasing time order
    assert checkpoints["100%"] is not None  # gap_size=0.008, deepest retracement=0.009 -> fully filled


def test_mae_mfe_bullish_and_bearish():
    m1 = pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01", periods=5, freq="1min", tz="UTC"),
        "open": [1.10] * 5, "high": [1.101, 1.103, 1.098, 1.104, 1.099],
        "low": [1.099, 1.100, 1.096, 1.101, 1.097], "close": [1.10] * 5,
    })
    entry_ts = m1["timestamp"].iloc[0]
    result = _mae_mfe(m1, entry_ts, entry_price=1.10, direction="bullish", horizon_timestamp=None)
    assert result["mfe"] == pytest.approx(1.104 - 1.10)
    assert result["mae"] == pytest.approx(1.10 - 1.096)

    result_bear = _mae_mfe(m1, entry_ts, entry_price=1.10, direction="bearish", horizon_timestamp=None)
    assert result_bear["mfe"] == pytest.approx(1.10 - 1.096)
    assert result_bear["mae"] == pytest.approx(1.104 - 1.10)


# ---------------------------------------------------------------------------
# Unit-level: two/third BOS detection
# ---------------------------------------------------------------------------


def test_two_consecutive_bos_pair_detected():
    events = pd.DataFrame([
        {"event_type": "BOS", "direction": "bullish", "break_candle_timestamp": pd.Timestamp("2024-01-01 00:00", tz="UTC"), "break_candle_close": 1.10},
        {"event_type": "BOS", "direction": "bullish", "break_candle_timestamp": pd.Timestamp("2024-01-01 01:00", tz="UTC"), "break_candle_close": 1.12},
        {"event_type": "CHoCH", "direction": "bearish", "break_candle_timestamp": pd.Timestamp("2024-01-01 02:00", tz="UTC"), "break_candle_close": 1.09},
    ])
    pairs = _find_bos_pairs(events)
    assert len(pairs) == 1
    first, second = pairs[0]
    assert first["break_candle_close"] == 1.10
    assert second["break_candle_close"] == 1.12


def test_bos_choch_pair_not_counted_as_two_bos():
    events = pd.DataFrame([
        {"event_type": "BOS", "direction": "bullish", "break_candle_timestamp": pd.Timestamp("2024-01-01 00:00", tz="UTC"), "break_candle_close": 1.10},
        {"event_type": "CHoCH", "direction": "bearish", "break_candle_timestamp": pd.Timestamp("2024-01-01 01:00", tz="UTC"), "break_candle_close": 1.05},
    ])
    assert _find_bos_pairs(events) == []


def test_third_bos_candidate_recorded_in_signals(all_signals):
    s2_signals = [s for s in all_signals if s.strategy_id == "S2"]
    assert len(s2_signals) > 0
    for s in s2_signals:
        assert "bos3_occurred" in s.metadata
        assert isinstance(s.metadata["bos3_occurred"], bool)


# ---------------------------------------------------------------------------
# Unit-level: confidence scoring + reason codes
# ---------------------------------------------------------------------------


def test_confidence_scoring_is_deterministic_and_explainable():
    factors = {"FreshOrderBlock": 1.0, "CHoCHConfirmation": 1.0, "GapQuality": 0.5}
    score1, contrib1 = compute_confidence(factors)
    score2, contrib2 = compute_confidence(factors)
    assert score1 == score2
    assert 0 <= score1 <= 100
    assert set(contrib1.keys()) == set(factors.keys())
    for name, info in contrib1.items():
        assert info["contribution"] == pytest.approx(info["weight"] * info["value"])


def test_confidence_scoring_rewards_more_satisfied_factors():
    low, _ = compute_confidence({"FreshOrderBlock": 0.0, "CHoCHConfirmation": 0.0})
    high, _ = compute_confidence({"FreshOrderBlock": 1.0, "CHoCHConfirmation": 1.0})
    assert high > low


def test_reason_codes_format():
    codes = build_reason_codes("S1", ["GapDetected", "GapAboveMinimum"], 82.4)
    assert codes[0] == "S1"
    assert codes[-1] == "Confidence82"
    assert "GapDetected" in codes and "GapAboveMinimum" in codes


# ---------------------------------------------------------------------------
# Integration-level: signal generation, schema, dedup, config, reproducibility
# ---------------------------------------------------------------------------


def test_signal_generation_produces_valid_schema(all_signals):
    assert len(all_signals) > 0
    for s in all_signals:
        d = s.to_dict()
        for field_name in REQUIRED_SIGNAL_FIELDS:
            assert field_name in d, f"missing field {field_name}"
        assert s.direction in ("bullish", "bearish")
        assert s.strategy_id in STRATEGY_MODULES
        assert len(s.entry_zone) == 2
        assert s.entry_zone[1] >= s.entry_zone[0]
        assert isinstance(s.reason_codes, list) and s.reason_codes[0] == s.strategy_id


def test_signal_dataframe_export(all_signals):
    df = signals_to_dataframe(all_signals)
    assert len(df) == len(all_signals)
    assert "confidence_score" in df.columns


def test_no_duplicate_signals(all_signals):
    keys = [(s.strategy_id, s.symbol, s.timeframe, s.timestamp, s.direction) for s in all_signals]
    assert len(keys) == len(set(keys))


def test_dedupe_signals_helper_removes_exact_duplicates():
    sig = Signal(
        signal_id="X", strategy_id="S1", timestamp=pd.Timestamp("2024-01-01", tz="UTC"),
        symbol="TEST", timeframe="M1", direction="bullish", entry_zone=(1.0, 1.1),
        stop_loss_reference=0.99, target_reference=1.2, confidence_score=80.0,
        reason_codes=["S1"], confluence_snapshot={}, market_structure_state="BULLISH",
        session=None, risk_reference={}, metadata={},
    )
    deduped = dedupe_signals([sig, sig])
    assert len(deduped) == 1


def test_signal_suppression_via_confidence_threshold():
    # Use a small, fast, dedicated slice rather than the big shared fixture:
    # with an unreachable confidence threshold, the strategy's per-level
    # scan can never early-exit via its normal "break on first match", so
    # running this against the full 10-day fixture is needlessly slow.
    from src.strategies import s3_liquidity_sweep

    small_m1 = make_multi_day_m1(num_days=2)
    small_context = MarketContext(symbol="TEST", m1=small_m1)
    normal_signals = s3_liquidity_sweep.generate_signals(small_context, S3Config())
    assert len(normal_signals) > 0  # sanity: this slice does produce signals normally

    # Task 11 Phase 1 fix note: FVGAlignment can now genuinely reach 1.0
    # (previously capped at 0.5 whenever require_fvg=False, discarding
    # real information -- see common.py/s3_liquidity_sweep.py). The true
    # maximum achievable score is 100.0 (every factor at weight*1.0), so
    # 99.9 is no longer a reliably unreachable threshold; use a value
    # strictly above the true maximum instead.
    high_threshold_config = S3Config(confidence_threshold=100.1)
    suppressed_signals = s3_liquidity_sweep.generate_signals(small_context, high_threshold_config)
    assert suppressed_signals == []


def test_configuration_override_disables_strategy(market_context):
    configs = {"S1": S1Config(enabled=False)}
    signals = run_strategies(market_context, configs=configs)
    assert all(s.strategy_id != "S1" for s in signals)


def test_signal_reproducibility(market_context):
    signals_a = run_strategies(market_context)
    signals_b = run_strategies(market_context)
    ids_a = [s.signal_id for s in signals_a]
    ids_b = [s.signal_id for s in signals_b]
    assert ids_a == ids_b
    scores_a = [s.confidence_score for s in signals_a]
    scores_b = [s.confidence_score for s in signals_b]
    assert scores_a == scores_b


def test_no_lookahead_bias_truncated_history_matches_prefix_of_full_run():
    """Signals generated strictly before a cutoff timestamp must be
    identical whether the strategy sees the full dataset or only data up
    to that cutoff -- the defining anti-look-ahead property.

    Compares on (strategy_id, timestamp, direction, confidence_score), not
    raw `signal_id`: the id embeds a per-call sequence counter that is an
    enumeration-order artifact, not part of a signal's semantic identity,
    and will legitimately differ between the two runs even when the
    underlying signals are identical.

    Uses `LiquidityConfig(equal_level_tolerance=0.0)`: with the default
    tolerance, src.features.liquidity's equal-level clustering (a Task 2
    batch-engine property, not something Task 3 introduces) sorts swings
    by PRICE rather than time and can merge a much-later swing into an
    earlier level's cluster, shifting that level's averaged price using
    information not yet available at the earlier timestamp. This is a
    known limitation of the batch liquidity engine (see
    docs/STRATEGY_ENGINE.md); disabling clustering isolates the property
    this test actually targets -- whether the STRATEGY layer introduces
    any additional look-ahead beyond what the underlying engines already
    have documented.
    """
    from config.settings import LiquidityConfig

    m1 = make_multi_day_m1(num_days=10)
    context = MarketContext(symbol="TEST", m1=m1, liquidity_config=LiquidityConfig(equal_level_tolerance=0.0))
    full_signals = run_strategies(context)
    cutoff = m1["timestamp"].iloc[len(m1) // 2]

    truncated_m1 = m1[m1["timestamp"] <= cutoff].reset_index(drop=True)
    truncated_context = MarketContext(symbol="TEST", m1=truncated_m1, liquidity_config=LiquidityConfig(equal_level_tolerance=0.0))
    truncated_signals = run_strategies(truncated_context)

    def key(s):
        return (s.strategy_id, s.timestamp, s.direction, s.confidence_score)

    full_before_cutoff = {key(s) for s in full_signals if s.timestamp <= cutoff}
    truncated_keys = {key(s) for s in truncated_signals}
    assert truncated_keys == full_before_cutoff
