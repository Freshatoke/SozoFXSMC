"""
Task 12.4 -- tests for the causal (point-in-time) liquidity reference,
src.research.robustness.causal_liquidity.

Two things are being verified:

1. The causal reference itself has no look-ahead: an as_of_index query
   on the full dataframe must equal the full computation on a dataframe
   physically truncated to that same cutoff (the same invariant already
   used throughout tests/test_no_lookahead.py).

2. The batch reference implementation (src.features.liquidity, NOT
   modified by this task) is DEMONSTRABLY not causally valid: a level
   can be reported as SWEPT at a timestamp that is BEFORE one of its own
   cluster members was even confirmed, because clustering is done in one
   global, price-sorted pass over every swing in the as_of window,
   blind to time order. This locks in that known, documented limitation
   as a regression test without changing batch's behavior.
"""

import pandas as pd

from config.settings import LiquidityConfig
from src.features.liquidity import detect_liquidity_levels
from src.research.robustness.causal_liquidity import detect_causal_liquidity_levels
from tests.helpers import make_candles


def _sweep_then_reform_rows():
    """H1 (swing high @ 1.20, confirmed at candle 3) is swept by candle 4
    (high 1.25 > 1.20, close 1.15 < 1.20) well before H2 (swing high @
    1.205, confirmed at candle 9) ever forms. H1 and H2 are within
    LiquidityConfig's default-scale tolerance of each other in price."""
    return [
        (1.00, 1.00, 0.99, 1.00),   # 0 filler
        (1.00, 1.05, 0.99, 1.00),   # 1 left-of-H1
        (1.00, 1.20, 0.95, 1.05),   # 2 H1 peak (1.20)
        (1.05, 1.10, 1.00, 1.05),   # 3 right-of-H1 / confirms H1
        (1.05, 1.25, 1.00, 1.15),   # 4 sweeps H1 (high>1.20, close<1.20)
        (1.15, 1.15, 1.05, 1.10),   # 5 filler
        (1.10, 1.12, 1.05, 1.10),   # 6 filler
        (1.10, 1.10, 1.05, 1.08),   # 7 left-of-H2
        (1.08, 1.205, 1.05, 1.15),  # 8 H2 peak (1.205, within tol of H1)
        (1.15, 1.15, 1.05, 1.10),   # 9 right-of-H2 / confirms H2
        (1.10, 1.10, 1.05, 1.08),   # 10 trailing filler so H2's confirmation is visible
    ]


def test_causal_liquidity_as_of_index_matches_truncated_dataframe():
    rows = _sweep_then_reform_rows()
    df_full = make_candles(rows)
    df_truncated = df_full.iloc[:8].reset_index(drop=True)  # up to and including index 7
    cfg = LiquidityConfig(swing_left=1, swing_right=1, equal_level_tolerance=0.01)

    levels_cutoff = detect_causal_liquidity_levels(df_full, "TEST", "M1", config=cfg, as_of_index=7)
    levels_truncated = detect_causal_liquidity_levels(df_truncated, "TEST", "M1", config=cfg)

    cols = ["type", "side", "price", "state", "creation_timestamp", "swept_timestamp"]
    pd.testing.assert_frame_equal(
        levels_cutoff[cols].reset_index(drop=True),
        levels_truncated[cols].reset_index(drop=True),
    )


def test_causal_liquidity_does_not_reopen_a_swept_level():
    """The core Task 12.4 finding: a swept level must not be able to
    merge with a later-confirmed swing. H1 and H2 must remain two
    separate, correctly-timestamped levels.

    (The sweep candle itself, index 4 with high=1.25, also happens to
    qualify as its own unrelated swing high -- far outside tolerance of
    H1/H2's ~1.20 price -- so it is filtered out below rather than
    asserting a total count of 2.)"""
    rows = _sweep_then_reform_rows()
    df = make_candles(rows)
    cfg = LiquidityConfig(swing_left=1, swing_right=1, equal_level_tolerance=0.01)

    levels = detect_causal_liquidity_levels(df, "TEST", "M1", config=cfg)
    highs = levels[(levels["side"] == "buy_side") & (levels["price"] < 1.21)].sort_values("creation_timestamp")

    assert len(highs) == 2, "H1 and H2 must remain separate levels, not merged into one equal_high"
    h1, h2 = highs.iloc[0], highs.iloc[1]
    assert h1["price"] == 1.20
    assert h1["state"] == "SWEPT"
    assert h1["swept_timestamp"] == df["timestamp"].iloc[4]
    assert h2["price"] == 1.205
    assert h2["state"] == "ACTIVE"
    # H2 cannot have been "created" before its own confirming candle closed.
    assert h2["creation_timestamp"] > h1["swept_timestamp"]


def test_batch_reference_incorrectly_merges_a_swept_level_with_a_later_swing():
    """Documents (without modifying) the batch detector's known look-ahead
    limitation: src.features.liquidity.detect_liquidity_levels clusters
    ALL swings in the as_of window in one global, price-sorted pass, so a
    swing confirmed AFTER a level was already swept can still retroactively
    merge into it -- producing a `swept_timestamp` that predates the
    confirmation of one of the level's own cluster members. This is why
    Task 12.4 does not target parity with batch's liquidity output; the
    causal reference (test_causal_liquidity_does_not_reopen_a_swept_level,
    above) is the correct target instead.

    (The sweep candle itself, index 4 with high=1.25, also happens to
    qualify as its own unrelated swing high -- far outside tolerance of
    H1/H2's ~1.20 price -- so it is filtered out below rather than
    asserting a total count of 1.)"""
    rows = _sweep_then_reform_rows()
    df = make_candles(rows)
    cfg = LiquidityConfig(swing_left=1, swing_right=1, equal_level_tolerance=0.01)

    levels = detect_liquidity_levels(df, "TEST", "M1", config=cfg)
    highs = levels[(levels["side"] == "buy_side") & (levels["price"] < 1.21)]

    assert len(highs) == 1, "batch incorrectly merges H1 and H2 into a single cluster"
    merged = highs.iloc[0]
    assert merged["type"] == "equal_high"
    h2_confirmed_timestamp = df["timestamp"].iloc[9] + pd.Timedelta(minutes=1)
    assert merged["swept_timestamp"] < h2_confirmed_timestamp, (
        "batch reports this level as already swept BEFORE one of its own "
        "cluster members (H2) was even confirmed -- the look-ahead this "
        "task documents"
    )
