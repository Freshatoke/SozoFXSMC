from config.settings import FVGConfig
from src.features.fvg import detect_fvgs
from tests.helpers import make_candles


def test_bullish_fvg_creation():
    rows = [
        (1.1000, 1.1010, 1.0990, 1.1005),  # i-1: high=1.1010
        (1.1005, 1.1300, 1.1000, 1.1250),  # i: impulsive bullish candle
        (1.1250, 1.1400, 1.1200, 1.1350),  # i+1: low=1.1200 > 1.1010 -> gap
    ]
    df = make_candles(rows)
    fvgs = detect_fvgs(df, "TEST", "M1")
    assert len(fvgs) == 1
    fvg = fvgs.iloc[0]
    assert fvg["direction"] == "bullish"
    assert fvg["bottom"] == 1.1010
    assert fvg["top"] == 1.1200
    assert fvg["size"] > 0


def test_bearish_fvg_creation():
    rows = [
        (1.1010, 1.1020, 1.1000, 1.1005),  # i-1: low=1.1000
        (1.1005, 1.1010, 1.0800, 1.0850),  # i: impulsive bearish candle
        (1.0850, 1.0900, 1.0700, 1.0750),  # i+1: high=1.0900 < 1.1000 -> gap
    ]
    df = make_candles(rows)
    fvgs = detect_fvgs(df, "TEST", "M1")
    assert len(fvgs) == 1
    fvg = fvgs.iloc[0]
    assert fvg["direction"] == "bearish"
    assert fvg["top"] == 1.1000
    assert fvg["bottom"] == 1.0900


def test_fvg_partial_fill():
    rows = [
        (1.1000, 1.1010, 1.0990, 1.1005),
        (1.1005, 1.1300, 1.1000, 1.1250),
        (1.1250, 1.1400, 1.1200, 1.1350),   # bullish FVG [1.1010, 1.1200]
        (1.1350, 1.1360, 1.1150, 1.1160),   # trades back into the zone partially (low=1.1150)
    ]
    df = make_candles(rows)
    fvgs = detect_fvgs(df, "TEST", "M1")
    fvg = fvgs.iloc[0]
    assert fvg["mitigation_state"] == "PARTIALLY_FILLED"
    assert 0 < fvg["filled_percentage"] < 100


def test_fvg_complete_mitigation():
    rows = [
        (1.1000, 1.1010, 1.0990, 1.1005),
        (1.1005, 1.1300, 1.1000, 1.1250),
        (1.1250, 1.1400, 1.1200, 1.1350),   # bullish FVG [1.1010, 1.1200]
        (1.1350, 1.1360, 1.1005, 1.1010),   # trades all the way through the zone (low <= 1.1010)
    ]
    df = make_candles(rows)
    fvgs = detect_fvgs(df, "TEST", "M1")
    fvg = fvgs.iloc[0]
    assert fvg["mitigation_state"] == "FULLY_MITIGATED"
    assert fvg["filled_percentage"] == 100.0


def test_no_fvg_when_no_gap():
    rows = [
        (1.1000, 1.1010, 1.0990, 1.1005),
        (1.1005, 1.1020, 1.0995, 1.1010),
        (1.1010, 1.1030, 1.1000, 1.1015),
    ]
    df = make_candles(rows)
    fvgs = detect_fvgs(df, "TEST", "M1")
    assert len(fvgs) == 0
