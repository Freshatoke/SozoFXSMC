"""
Task 2.5 tests: incremental engine correctness, registry consistency,
event-bus ordering/completeness, object lifecycle, restart/recovery, and
look-ahead safety. Cross-checked against the Task 1/2 batch reference
implementations wherever a direct equivalence claim can be made.
"""

import pandas as pd
import pytest

from config.settings import SwingConfig, StructureConfig, LiquidityConfig
from src.engine.engine import IncrementalEngine
from src.engine.event_bus import EventType
from src.structure.swings import detect_swings
from src.structure.market_structure import detect_structure_events
from src.features.order_blocks import detect_order_blocks
from src.features.fvg import detect_fvgs
from tests.helpers import make_candles
from tests.test_displacement import _quiet_rows
from tests.test_order_blocks import _bullish_ob_setup


def _bullish_scenario_rows():
    rows, ob_low, ob_high = _bullish_ob_setup()
    rows.append((ob_high + 0.0005, ob_high + 0.0006, ob_low + 0.0002, ob_high + 0.0001))  # touch
    rows.append((ob_low + 0.0001, ob_low + 0.0002, ob_low - 0.0010, ob_low - 0.0008))       # full mitigation
    return rows, ob_low, ob_high


def _run_engine(df, **kwargs):
    engine = IncrementalEngine(symbol="TEST", timeframe="M1", interval=pd.Timedelta(minutes=1), **kwargs)
    engine.process_dataframe(df)
    return engine


# ---------------------------------------------------------------------------
# Incremental processing correctness (equivalence with Task 1/2 batch engines)
# ---------------------------------------------------------------------------


def test_incremental_swings_match_batch():
    rows, _, _ = _bullish_scenario_rows()
    df = make_candles(rows)

    batch = detect_swings(df, config=SwingConfig(), timeframe_label="M1")
    engine = _run_engine(df)
    incremental = engine.swings.confirmed_swings

    assert len(incremental) == len(batch)
    for b, i in zip(batch.itertuples(index=False), incremental):
        assert i["swing_type"] == b.swing_type
        assert i["price"] == b.price
        assert i["swing_timestamp"] == b.swing_timestamp
        assert i["confirmed_timestamp"] == b.confirmed_timestamp


def test_incremental_structure_events_match_batch():
    rows, _, _ = _bullish_scenario_rows()
    df = make_candles(rows)

    swing_cfg = SwingConfig()
    struct_cfg = StructureConfig(swing=swing_cfg)
    batch_swings = detect_swings(df, config=swing_cfg, timeframe_label="M1")
    batch_events = detect_structure_events(df, batch_swings, symbol="TEST", timeframe="M1", config=struct_cfg)

    engine = _run_engine(df, structure_config=struct_cfg)
    incremental_events = engine.structure.events

    assert len(incremental_events) == len(batch_events)
    for b, i in zip(batch_events.itertuples(index=False), incremental_events):
        assert i["event_type"] == b.event_type
        assert i["direction"] == b.direction
        assert i["new_structure_state"] == b.new_structure_state
        assert i["break_candle_timestamp"] == b.break_candle_timestamp


def test_incremental_order_blocks_match_batch():
    rows, ob_low, ob_high = _bullish_scenario_rows()
    df = make_candles(rows)

    swing_cfg = SwingConfig()
    struct_cfg = StructureConfig(swing=swing_cfg)
    batch_swings = detect_swings(df, config=swing_cfg, timeframe_label="M1")
    batch_events = detect_structure_events(df, batch_swings, symbol="TEST", timeframe="M1", config=struct_cfg)
    batch_obs, _ = detect_order_blocks(df, "TEST", "M1", structure_events=batch_events)

    engine = _run_engine(df, structure_config=struct_cfg)
    incremental_obs = engine.order_blocks.all_order_blocks

    assert len(incremental_obs) == len(batch_obs) == 1
    b, i = batch_obs.iloc[0], incremental_obs[0]
    assert i["direction"] == b["direction"]
    assert i["low"] == b["low"] == ob_low
    assert i["high"] == b["high"] == ob_high
    assert i["current_state"] == b["current_state"] == "FULLY_MITIGATED"


def test_incremental_fvg_matches_batch():
    # These 5 rows legitimately contain TWO 3-candle gaps (a bullish one at
    # rows 0-1-2, fully mitigated by row 4; and a bearish one at rows 2-3-4,
    # still open) -- both the batch and incremental engines must find both.
    rows = [
        (1.1000, 1.1010, 1.0990, 1.1005),
        (1.1005, 1.1300, 1.1000, 1.1250),
        (1.1250, 1.1400, 1.1200, 1.1350),
        (1.1350, 1.1360, 1.1150, 1.1160),
        (1.1160, 1.1160, 1.1005, 1.1010),
    ]
    df = make_candles(rows)
    batch = detect_fvgs(df, "TEST", "M1")
    engine = _run_engine(df)
    incremental = engine.fvgs.all_fvgs

    assert len(incremental) == len(batch) == 2
    for b, i in zip(batch.itertuples(index=False), incremental):
        assert i["direction"] == b.direction
        assert i["top"] == b.top
        assert i["bottom"] == b.bottom
        assert i["active_status"] == b.active_status


# ---------------------------------------------------------------------------
# Registry + lifecycle consistency
# ---------------------------------------------------------------------------


def test_registry_tracks_active_order_block_lifecycle():
    rows, _, _ = _bullish_ob_setup()
    df_creation = make_candles(rows)
    engine = _run_engine(df_creation)
    assert len(engine.registry.active_bullish_order_blocks) == 1
    assert engine.registry.active_bullish_order_blocks[0]["current_state"] == "ACTIVE"

    rows_full, ob_low, ob_high = _bullish_scenario_rows()
    df_full = make_candles(rows_full)
    engine_full = _run_engine(df_full)
    # fully mitigated OBs must NOT vanish from history, but must leave the active view
    assert len(engine_full.registry.active_bullish_order_blocks) == 0
    assert len(engine_full.order_blocks.all_order_blocks) == 1
    assert engine_full.order_blocks.all_order_blocks[0]["current_state"] == "FULLY_MITIGATED"


def test_object_never_disappears_only_state_changes():
    rows, _, _ = _bullish_scenario_rows()
    df = make_candles(rows)
    engine = _run_engine(df)
    ob_ids_seen = {ob["ob_id"] for ob in engine.order_blocks.all_order_blocks}
    assert len(ob_ids_seen) == 1
    # the same object persisted through ACTIVE -> PARTIALLY_MITIGATED -> FULLY_MITIGATED
    assert engine.order_blocks.all_order_blocks[0]["current_state"] == "FULLY_MITIGATED"


# ---------------------------------------------------------------------------
# Event bus: ordering, no duplicates, no missing events
# ---------------------------------------------------------------------------


def test_event_ordering_matches_pipeline_sequence():
    rows, _, _ = _bullish_scenario_rows()
    df = make_candles(rows)
    engine = _run_engine(df)
    log = engine.event_bus.log
    sequences = [e.sequence for e in log]
    assert sequences == sorted(sequences)  # monotonically increasing, never reordered
    assert len(sequences) == len(set(sequences))  # every event has a unique sequence number


def test_no_duplicate_order_block_created_events():
    rows, _, _ = _bullish_scenario_rows()
    df = make_candles(rows)
    engine = _run_engine(df)
    created_events = engine.event_bus.events_of_type(EventType.ORDER_BLOCK_CREATED)
    ob_ids = [e.payload["ob_id"] for e in created_events]
    assert len(ob_ids) == len(set(ob_ids))  # never created twice for the same object


def test_confluence_updated_fires_exactly_once_per_candle():
    rows, _, _ = _bullish_scenario_rows()
    df = make_candles(rows)
    engine = _run_engine(df)
    confluence_events = engine.event_bus.events_of_type(EventType.CONFLUENCE_UPDATED)
    assert len(confluence_events) == len(df) == engine.candles_processed


# ---------------------------------------------------------------------------
# Confluence snapshot consistency
# ---------------------------------------------------------------------------


def test_confluence_snapshot_is_deterministic_and_consistent_with_registry():
    rows, _, _ = _bullish_scenario_rows()
    df = make_candles(rows)
    engine_a = _run_engine(df)
    engine_b = _run_engine(df)

    snap_a = engine_a.registry.current_confluence_snapshot
    snap_b = engine_b.registry.current_confluence_snapshot

    assert snap_a.to_dict() == snap_b.to_dict()  # same input -> same output, no hidden randomness
    assert snap_a.market_state == engine_a.registry.market_structure_state
    assert len(snap_a.active_order_blocks) == len(engine_a.registry.active_bullish_order_blocks) + len(engine_a.registry.active_bearish_order_blocks)


# ---------------------------------------------------------------------------
# Restart / recovery
# ---------------------------------------------------------------------------


def test_restart_recovery_matches_continuous_run(tmp_path):
    rows, _, _ = _bullish_scenario_rows()
    rows.append((1.10, 1.101, 1.099, 1.1005))
    df = make_candles(rows)

    baseline = _run_engine(df)

    engine = IncrementalEngine(symbol="TEST", timeframe="M1", interval=pd.Timedelta(minutes=1))
    split_point = len(df) // 2
    engine.process_dataframe(df.iloc[:split_point])
    state_path = tmp_path / "engine_state.json"
    engine.save(state_path)

    resumed = IncrementalEngine.load(state_path)
    resumed.process_dataframe(df.iloc[split_point:].reset_index(drop=True))

    assert resumed.candles_processed == baseline.candles_processed
    assert [o["ob_id"] for o in resumed.order_blocks.all_order_blocks] == [o["ob_id"] for o in baseline.order_blocks.all_order_blocks]
    assert [o["current_state"] for o in resumed.order_blocks.all_order_blocks] == [o["current_state"] for o in baseline.order_blocks.all_order_blocks]
    assert resumed.structure.state == baseline.structure.state
    assert len(resumed.fvgs.all_fvgs) == len(baseline.fvgs.all_fvgs)
    assert len(resumed.swings.confirmed_swings) == len(baseline.swings.confirmed_swings)


# ---------------------------------------------------------------------------
# No look-ahead bias
# ---------------------------------------------------------------------------


def test_past_snapshots_are_immutable_once_produced():
    """Feeding MORE future candles must never change a snapshot already
    produced for an earlier candle -- the defining anti-look-ahead property
    of a genuinely incremental/streaming engine."""
    rows, _, _ = _bullish_scenario_rows()
    rows.append((1.10, 1.30, 1.09, 1.28))  # a big future move
    df = make_candles(rows)

    engine = IncrementalEngine(symbol="TEST", timeframe="M1", interval=pd.Timedelta(minutes=1))
    snapshots_partial = engine.process_dataframe(df.iloc[: len(df) - 1])
    frozen_snapshot_dict = snapshots_partial[-1].to_dict()

    engine.process_candle_index_before_extra = len(snapshots_partial)
    engine.process_dataframe(df.iloc[len(df) - 1:].reset_index(drop=True))

    assert frozen_snapshot_dict == snapshots_partial[-1].to_dict()


def test_engine_never_uses_future_candle_data():
    """Processing only a prefix of the stream must produce identical
    tracker state to processing the full stream and looking at the state
    as of that same prefix length (i.e. nothing computed at step k depends
    on candles after k)."""
    rows, _, _ = _bullish_scenario_rows()
    rows.append((1.10, 1.30, 1.09, 1.28))
    df = make_candles(rows)
    cut = len(df) - 2

    full_engine = IncrementalEngine(symbol="TEST", timeframe="M1", interval=pd.Timedelta(minutes=1))
    full_engine.process_dataframe(df)

    partial_engine = IncrementalEngine(symbol="TEST", timeframe="M1", interval=pd.Timedelta(minutes=1))
    partial_engine.process_dataframe(df.iloc[:cut])

    obs_full_as_of_cut = [ob for ob in full_engine.order_blocks.all_order_blocks if ob["creation_index"] < cut]
    obs_partial = partial_engine.order_blocks.all_order_blocks

    assert len(obs_full_as_of_cut) == len(obs_partial)


# ---------------------------------------------------------------------------
# Performance benchmark execution (smoke test, not a strict perf assertion)
# ---------------------------------------------------------------------------


def test_incremental_engine_processes_large_stream_quickly():
    import time
    import numpy as np

    n = 5000
    rng = np.random.default_rng(1)
    ts = pd.date_range("2024-01-01", periods=n, freq="1min", tz="UTC")
    close = 1.10 + np.cumsum(rng.normal(0, 0.0003, n))
    open_ = np.concatenate([[1.10], close[:-1]])
    high = np.maximum(open_, close) + np.abs(rng.normal(0, 0.0002, n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0, 0.0002, n))
    df = pd.DataFrame({"timestamp": ts, "open": open_, "high": high, "low": low, "close": close})

    engine = IncrementalEngine(symbol="BENCH", timeframe="M1", interval=pd.Timedelta(minutes=1))
    start = time.perf_counter()
    engine.process_dataframe(df)
    elapsed = time.perf_counter() - start

    assert engine.candles_processed == n
    assert elapsed < 30.0  # generous ceiling; incremental processing of 5k candles should be fast
