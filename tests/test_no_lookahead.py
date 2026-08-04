"""
Verifies that every feature engine's `as_of_index` cutoff genuinely blocks
future information: a snapshot computed with `as_of_index=k` on the FULL
dataframe must be identical to the same snapshot computed on a dataframe
physically truncated to the first k+1 rows. If future rows could change
the result, the engine would be leaking look-ahead information.
"""

import pandas as pd

from src.features.order_blocks import detect_order_blocks
from src.features.fvg import detect_fvgs
from src.features.liquidity import detect_liquidity_levels
from tests.helpers import make_candles
from tests.test_order_blocks import _bullish_ob_setup


def test_order_blocks_as_of_index_matches_truncated_dataframe():
    rows, _, _ = _bullish_ob_setup(extra_rows=[
        (1.30, 1.31, 1.28, 1.29),
        (1.29, 1.30, 1.20, 1.21),   # a further move that must NOT affect the as_of=21 snapshot
    ])
    df_full = make_candles(rows)
    df_truncated = df_full.iloc[:22].reset_index(drop=True)  # up to and including index 21

    obs_cutoff, _ = detect_order_blocks(df_full, "TEST", "M1", as_of_index=21)
    obs_truncated, _ = detect_order_blocks(df_truncated, "TEST", "M1")

    pd.testing.assert_frame_equal(
        obs_cutoff.drop(columns=["displacement_reference"]),
        obs_truncated.drop(columns=["displacement_reference"]),
    )


def test_fvg_as_of_index_matches_truncated_dataframe():
    rows = [
        (1.1000, 1.1010, 1.0990, 1.1005),
        (1.1005, 1.1300, 1.1000, 1.1250),
        (1.1250, 1.1400, 1.1200, 1.1350),
        (1.1350, 1.1360, 1.1150, 1.1160),
        (1.1160, 1.1160, 1.1005, 1.1010),  # future candle that fully mitigates -- must not leak backward
    ]
    df_full = make_candles(rows)
    df_truncated = df_full.iloc[:4].reset_index(drop=True)

    fvgs_cutoff = detect_fvgs(df_full, "TEST", "M1", as_of_index=3)
    fvgs_truncated = detect_fvgs(df_truncated, "TEST", "M1")

    pd.testing.assert_frame_equal(fvgs_cutoff, fvgs_truncated)


def test_liquidity_as_of_index_matches_truncated_dataframe():
    from config.settings import LiquidityConfig
    rows = [
        (0.95, 1.00, 0.90, 0.95),
        (1.00, 1.20, 0.95, 1.05),
        (1.05, 1.10, 1.00, 1.05),
        (1.05, 1.10, 1.00, 1.05),
        (1.05, 1.25, 1.00, 1.15),  # future sweep candle -- must not leak backward
    ]
    df_full = make_candles(rows)
    df_truncated = df_full.iloc[:4].reset_index(drop=True)
    cfg = LiquidityConfig(swing_left=1, swing_right=1)

    levels_cutoff = detect_liquidity_levels(df_full, "TEST", "M1", config=cfg, as_of_index=3)
    levels_truncated = detect_liquidity_levels(df_truncated, "TEST", "M1", config=cfg)

    pd.testing.assert_frame_equal(levels_cutoff, levels_truncated)
