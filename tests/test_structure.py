import pandas as pd

from config.settings import SwingConfig, StructureConfig
from src.structure.swings import detect_swings
from src.structure.market_structure import detect_structure_events
from tests.helpers import make_candles

CFG = StructureConfig(swing=SwingConfig(left=1, right=1))


def _run(rows):
    df = make_candles(rows)
    swings = detect_swings(df, config=CFG.swing, timeframe_label="M1")
    events = detect_structure_events(df, swings, symbol="TEST", timeframe="M1", config=CFG)
    return df, swings, events


def test_bullish_bos_on_close_beyond_swing_high():
    rows = [
        (0.95, 1.00, 0.90, 0.95),
        (1.00, 1.20, 0.95, 1.05),  # swing high at index 1 (1.20)
        (1.05, 1.10, 1.00, 1.05),
        (1.05, 1.25, 1.00, 1.25),  # closes above 1.20 -> bullish BOS
    ]
    _, _, events = _run(rows)
    assert len(events) == 1
    ev = events.iloc[0]
    assert ev["event_type"] == "BOS"
    assert ev["direction"] == "bullish"
    assert ev["previous_structure_state"] == "UNKNOWN"
    assert ev["new_structure_state"] == "BULLISH"
    assert ev["broken_level"] == 1.20


def test_bearish_bos_on_close_beyond_swing_low():
    rows = [
        (1.05, 1.10, 1.00, 1.05),
        (1.00, 1.05, 0.80, 0.95),  # swing low at index 1 (0.80)
        (0.95, 1.00, 0.90, 0.95),
        (0.90, 0.95, 0.75, 0.75),  # closes below 0.80 -> bearish BOS
    ]
    _, _, events = _run(rows)
    assert len(events) == 1
    ev = events.iloc[0]
    assert ev["event_type"] == "BOS"
    assert ev["direction"] == "bearish"
    assert ev["previous_structure_state"] == "UNKNOWN"
    assert ev["new_structure_state"] == "BEARISH"
    assert ev["broken_level"] == 0.80


def test_bullish_choch_after_bearish_state():
    rows = [
        # establish bearish BOS first (state -> BEARISH)
        (1.05, 1.10, 1.00, 1.05),
        (1.00, 1.05, 0.80, 0.95),   # swing low idx1 = 0.80
        (0.95, 1.00, 0.90, 0.95),
        (0.90, 0.95, 0.75, 0.75),   # break below 0.80 -> BOS bearish, state=BEARISH
        # now build a swing high and break above it -> CHoCH bullish
        (0.75, 0.90, 0.70, 0.85),
        (0.85, 1.30, 0.80, 0.90),   # swing high idx5 = 1.30
        (0.90, 1.00, 0.85, 0.90),
        (0.90, 1.35, 0.85, 1.35),   # closes above 1.30 -> bullish CHoCH
    ]
    _, _, events = _run(rows)
    assert len(events) == 2
    ev2 = events.iloc[1]
    assert ev2["event_type"] == "CHoCH"
    assert ev2["direction"] == "bullish"
    assert ev2["previous_structure_state"] == "BEARISH"
    assert ev2["new_structure_state"] == "BULLISH"


def test_bearish_choch_after_bullish_state():
    rows = [
        # establish bullish BOS first (state -> BULLISH)
        (0.95, 1.00, 0.90, 0.95),
        (1.00, 1.20, 0.95, 1.05),   # swing high idx1 = 1.20
        (1.05, 1.10, 1.00, 1.05),
        (1.05, 1.25, 1.00, 1.25),   # break above 1.20 -> BOS bullish, state=BULLISH
        # now build a swing low and break below it -> CHoCH bearish
        (1.25, 1.30, 1.20, 1.25),
        (1.20, 1.25, 0.70, 1.15),   # swing low idx5 = 0.70
        (1.15, 1.20, 1.10, 1.15),
        (1.15, 1.20, 0.65, 0.65),   # closes below 0.70 -> bearish CHoCH
    ]
    _, _, events = _run(rows)
    assert len(events) == 2
    ev2 = events.iloc[1]
    assert ev2["event_type"] == "CHoCH"
    assert ev2["direction"] == "bearish"
    assert ev2["previous_structure_state"] == "BULLISH"
    assert ev2["new_structure_state"] == "BEARISH"


def test_wick_only_break_does_not_trigger_event():
    rows = [
        (0.95, 1.00, 0.90, 0.95),
        (1.00, 1.20, 0.95, 1.05),  # swing high idx1 = 1.20
        (1.05, 1.10, 1.00, 1.05),
        (1.05, 1.30, 1.00, 1.15),  # wicks to 1.30 but CLOSES at 1.15, below 1.20
    ]
    _, _, events = _run(rows)
    assert len(events) == 0


def test_close_beyond_level_does_trigger_event():
    rows = [
        (0.95, 1.00, 0.90, 0.95),
        (1.00, 1.20, 0.95, 1.05),  # swing high idx1 = 1.20
        (1.05, 1.10, 1.00, 1.05),
        (1.05, 1.30, 1.00, 1.21),  # closes at 1.21, beyond 1.20
    ]
    _, _, events = _run(rows)
    assert len(events) == 1
    assert events.iloc[0]["event_type"] == "BOS"


def test_duplicate_break_events_prevented():
    rows = [
        (0.95, 1.00, 0.90, 0.95),
        (1.00, 1.20, 0.95, 1.05),  # swing high idx1 = 1.20
        (1.05, 1.10, 1.00, 1.05),
        (1.05, 1.25, 1.00, 1.25),  # break above 1.20 -> BOS bullish
        (1.25, 1.30, 1.20, 1.28),  # still above 1.20, must NOT refire
        (1.28, 1.35, 1.22, 1.32),  # still above 1.20, must NOT refire
    ]
    _, _, events = _run(rows)
    assert len(events) == 1
