from config.settings import OrderBlockConfig, DisplacementConfig
from src.features.order_blocks import detect_order_blocks
from src.features.displacement import detect_displacement
from tests.helpers import make_candles
from tests.test_displacement import _quiet_rows


def _bullish_ob_setup(extra_rows=None):
    rows = _quiet_rows(20)
    last_close = rows[-1][3]
    # final bearish candle immediately before displacement -> becomes the bullish OB.
    # Kept small/wicky so it does NOT itself qualify as a displacement candle.
    bearish_ob_candle = (last_close, last_close + 0.0002, last_close - 0.0005, last_close - 0.0003)
    rows.append(bearish_ob_candle)
    ob_low = bearish_ob_candle[2]
    ob_high = bearish_ob_candle[1]
    disp_start_open = bearish_ob_candle[3]
    rows.append((disp_start_open, disp_start_open + 0.0210, disp_start_open - 0.0005, disp_start_open + 0.0200))
    if extra_rows:
        rows.extend(extra_rows)
    return rows, ob_low, ob_high


def test_order_block_creation():
    rows, ob_low, ob_high = _bullish_ob_setup()
    df = make_candles(rows)

    # As of the displacement candle itself (index 21, a single-candle
    # displacement), the OB has just been created and no further candles
    # have been processed yet, so it must be freshly ACTIVE/untouched.
    obs, skipped = detect_order_blocks(df, "TEST", "M1", as_of_index=21)
    assert len(obs) == 1
    ob = obs.iloc[0]
    assert ob["direction"] == "bullish"
    assert ob["low"] == ob_low
    assert ob["high"] == ob_high
    assert ob["current_state"] == "ACTIVE"
    assert ob["freshness_status"] == "FRESH"


def test_order_block_partial_and_full_mitigation():
    last_close_ref = _quiet_rows(20)[-1][3]
    rows, ob_low, ob_high = _bullish_ob_setup()
    # candle that wicks into the OB zone but doesn't close through -> PARTIALLY_MITIGATED
    touch_candle = (ob_high + 0.0005, ob_high + 0.0006, ob_low + 0.0002, ob_high + 0.0001)
    rows.append(touch_candle)
    df = make_candles(rows)
    obs, _ = detect_order_blocks(df, "TEST", "M1")
    ob = obs.iloc[0]
    assert ob["current_state"] == "PARTIALLY_MITIGATED"
    assert ob["freshness_status"] == "MITIGATED"

    # now a candle that CLOSES below the OB low -> FULLY_MITIGATED
    rows2 = list(rows)
    rows2.append((ob_low + 0.0001, ob_low + 0.0002, ob_low - 0.0010, ob_low - 0.0008))
    df2 = make_candles(rows2)
    obs2, _ = detect_order_blocks(df2, "TEST", "M1")
    ob2 = obs2.iloc[0]
    assert ob2["current_state"] == "FULLY_MITIGATED"
    assert ob2["full_mitigation_timestamp"] is not None


def test_order_block_invalidation_with_opposing_structure():
    from config.settings import StructureConfig, SwingConfig
    from src.structure.swings import detect_swings
    from src.structure.market_structure import detect_structure_events

    rows, ob_low, ob_high = _bullish_ob_setup()
    # fully mitigate
    rows.append((ob_low + 0.0001, ob_low + 0.0002, ob_low - 0.0010, ob_low - 0.0008))
    # a strong further bearish leg to produce a bearish structure break shortly after
    rows.append((ob_low - 0.0008, ob_low - 0.0007, ob_low - 0.0040, ob_low - 0.0035))
    df = make_candles(rows)

    swing_cfg = SwingConfig(left=1, right=1)
    struct_cfg = StructureConfig(swing=swing_cfg)
    swings = detect_swings(df, config=swing_cfg, timeframe_label="M1")
    structure_events = detect_structure_events(df, swings, symbol="TEST", timeframe="M1", config=struct_cfg)

    ob_cfg = OrderBlockConfig(invalidation_lookahead=5)
    obs, _ = detect_order_blocks(df, "TEST", "M1", config=ob_cfg, structure_events=structure_events)
    ob = obs.iloc[0]
    assert ob["current_state"] in ("INVALIDATED", "FULLY_MITIGATED")


def test_order_block_skipped_when_no_opposite_candle():
    # all bullish candles -> a bullish displacement has no preceding bearish candle
    rows = [(1.10 + i * 0.0002, 1.10 + i * 0.0002 + 0.0003, 1.10 + i * 0.0002 - 0.0001, 1.10 + (i + 1) * 0.0002) for i in range(5)]
    last_close = rows[-1][3]
    rows.append((last_close, last_close + 0.0210, last_close - 0.0005, last_close + 0.0200))
    df = make_candles(rows)
    obs, skipped = detect_order_blocks(df, "TEST", "M1", config=OrderBlockConfig(lookback_candles=3))
    assert len(obs) == 0
    assert len(skipped) == 1
