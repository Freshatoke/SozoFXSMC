from config.settings import LiquidityConfig
from src.features.liquidity import detect_liquidity_levels
from tests.helpers import make_candles


def _rows_from_highs(highs):
    return [(h - 0.005, h, h - 0.01, h - 0.005) for h in highs]


def test_equal_highs_detected():
    # two swing highs at ~1.20 (within tolerance), left=right=1
    highs = [1.00, 1.20, 1.05, 1.10, 1.201, 1.05, 1.00]
    df = make_candles(_rows_from_highs(highs))
    cfg = LiquidityConfig(swing_left=1, swing_right=1, equal_level_tolerance=0.005)
    levels = detect_liquidity_levels(df, "TEST", "M1", config=cfg)
    equal_highs = levels[levels.type == "equal_high"]
    assert len(equal_highs) == 1
    assert equal_highs.iloc[0]["number_of_touches"] == 2
    assert equal_highs.iloc[0]["side"] == "buy_side"


def test_equal_lows_detected():
    lows = [1.00, 0.80, 0.95, 0.90, 0.801, 0.95, 1.00]
    rows = [(l + 0.005, l + 0.01, l, l + 0.005) for l in lows]
    df = make_candles(rows)
    cfg = LiquidityConfig(swing_left=1, swing_right=1, equal_level_tolerance=0.005)
    levels = detect_liquidity_levels(df, "TEST", "M1", config=cfg)
    equal_lows = levels[levels.type == "equal_low"]
    assert len(equal_lows) == 1
    assert equal_lows.iloc[0]["number_of_touches"] == 2
    assert equal_lows.iloc[0]["side"] == "sell_side"


def test_liquidity_sweep_detected():
    # single swing high at 1.20, then later a candle wicks above and closes back below -> sweep
    rows = [
        (0.95, 1.00, 0.90, 0.95),
        (1.00, 1.20, 0.95, 1.05),   # swing high idx1 = 1.20 (left=1,right=1)
        (1.05, 1.10, 1.00, 1.05),
        (1.05, 1.10, 1.00, 1.05),
        (1.05, 1.25, 1.00, 1.15),   # wicks to 1.25 (above 1.20) but closes at 1.15 (below) -> sweep
    ]
    df = make_candles(rows)
    cfg = LiquidityConfig(swing_left=1, swing_right=1)
    levels = detect_liquidity_levels(df, "TEST", "M1", config=cfg)
    swing_highs = levels[levels.type == "swing_high"]
    assert len(swing_highs) == 1
    assert swing_highs.iloc[0]["state"] == "SWEPT"
    assert swing_highs.iloc[0]["swept_timestamp"] is not None


def test_no_sweep_when_price_stays_below_level():
    rows = [
        (0.95, 1.00, 0.90, 0.95),
        (1.00, 1.20, 0.95, 1.05),
        (1.05, 1.10, 1.00, 1.05),
        (1.05, 1.15, 1.00, 1.10),
    ]
    df = make_candles(rows)
    cfg = LiquidityConfig(swing_left=1, swing_right=1)
    levels = detect_liquidity_levels(df, "TEST", "M1", config=cfg)
    swing_highs = levels[levels.type == "swing_high"]
    assert swing_highs.iloc[0]["state"] == "ACTIVE"
