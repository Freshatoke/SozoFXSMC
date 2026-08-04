"""Object lifecycle transition tests: verify states progress in the
documented order and never skip/disappear silently as more candles arrive."""

from config.settings import LiquidityConfig
from src.features.order_blocks import detect_order_blocks
from src.features.fvg import detect_fvgs
from src.features.liquidity import detect_liquidity_levels
from tests.helpers import make_candles
from tests.test_order_blocks import _bullish_ob_setup


def test_order_block_lifecycle_progresses_active_to_fully_mitigated():
    rows, ob_low, ob_high = _bullish_ob_setup()
    disp_end_index = 21  # single-candle displacement at index 21

    df_at_creation = make_candles(rows)
    obs_creation, _ = detect_order_blocks(df_at_creation, "TEST", "M1", as_of_index=disp_end_index)
    assert obs_creation.iloc[0]["current_state"] == "ACTIVE"

    rows_touch = list(rows)
    rows_touch.append((ob_high + 0.0005, ob_high + 0.0006, ob_low + 0.0002, ob_high + 0.0001))
    df_touch = make_candles(rows_touch)
    obs_touch, _ = detect_order_blocks(df_touch, "TEST", "M1")
    assert obs_touch.iloc[0]["current_state"] == "PARTIALLY_MITIGATED"

    rows_full = list(rows_touch)
    rows_full.append((ob_low + 0.0001, ob_low + 0.0002, ob_low - 0.0010, ob_low - 0.0008))
    df_full = make_candles(rows_full)
    obs_full, _ = detect_order_blocks(df_full, "TEST", "M1")
    assert obs_full.iloc[0]["current_state"] == "FULLY_MITIGATED"

    # same ob_id across all three snapshots -- proves it's the same tracked object, not a new one
    assert obs_creation.iloc[0]["ob_id"] == obs_touch.iloc[0]["ob_id"] == obs_full.iloc[0]["ob_id"]


def test_fvg_lifecycle_progresses_active_to_partial_to_full():
    base_rows = [
        (1.1000, 1.1010, 1.0990, 1.1005),
        (1.1005, 1.1300, 1.1000, 1.1250),
        (1.1250, 1.1400, 1.1200, 1.1350),  # bullish FVG [1.1010, 1.1200]
    ]
    df_active = make_candles(base_rows)
    fvgs_active = detect_fvgs(df_active, "TEST", "M1")
    assert fvgs_active.iloc[0]["active_status"] == "ACTIVE"

    partial_rows = base_rows + [(1.1350, 1.1360, 1.1150, 1.1160)]
    df_partial = make_candles(partial_rows)
    fvgs_partial = detect_fvgs(df_partial, "TEST", "M1")
    assert fvgs_partial.iloc[0]["active_status"] == "PARTIALLY_FILLED"

    full_rows = partial_rows + [(1.1160, 1.1160, 1.1005, 1.1010)]
    df_full = make_candles(full_rows)
    fvgs_full = detect_fvgs(df_full, "TEST", "M1")
    assert fvgs_full.iloc[0]["active_status"] == "FULLY_MITIGATED"

    assert fvgs_active.iloc[0]["fvg_id"] == fvgs_partial.iloc[0]["fvg_id"] == fvgs_full.iloc[0]["fvg_id"]


def test_liquidity_lifecycle_progresses_active_to_swept():
    rows_before_sweep = [
        (0.95, 1.00, 0.90, 0.95),
        (1.00, 1.20, 0.95, 1.05),   # swing high idx1 = 1.20
        (1.05, 1.10, 1.00, 1.05),
        (1.05, 1.10, 1.00, 1.05),
    ]
    cfg = LiquidityConfig(swing_left=1, swing_right=1)
    df_active = make_candles(rows_before_sweep)
    levels_active = detect_liquidity_levels(df_active, "TEST", "M1", config=cfg)
    swing_high = levels_active[levels_active.type == "swing_high"].iloc[0]
    assert swing_high["state"] == "ACTIVE"

    rows_swept = rows_before_sweep + [(1.05, 1.25, 1.00, 1.15)]
    df_swept = make_candles(rows_swept)
    levels_swept = detect_liquidity_levels(df_swept, "TEST", "M1", config=cfg)
    swept_row = levels_swept[levels_swept.liquidity_id == swing_high["liquidity_id"]].iloc[0]
    assert swept_row["state"] == "SWEPT"
