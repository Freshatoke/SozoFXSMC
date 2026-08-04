from config.settings import EngulfingConfig
from src.features.engulfing import detect_engulfing
from tests.helpers import make_candles


def test_bullish_engulfing_detected():
    rows = [
        (1.10, 1.101, 1.098, 1.099),   # bearish candle: open=1.10, close=1.099
        (1.098, 1.103, 1.097, 1.102),  # bullish candle engulfing prior body [1.099,1.10]
    ]
    df = make_candles(rows)
    events = detect_engulfing(df)
    assert len(events) == 1
    assert events.iloc[0]["direction"] == "bullish"


def test_bearish_engulfing_detected():
    rows = [
        (1.098, 1.101, 1.097, 1.100),  # bullish candle: open=1.098, close=1.100
        (1.101, 1.102, 1.096, 1.097),  # bearish candle engulfing prior body [1.098,1.100]
    ]
    df = make_candles(rows)
    events = detect_engulfing(df)
    assert len(events) == 1
    assert events.iloc[0]["direction"] == "bearish"


def test_non_engulfing_candles_produce_no_event():
    rows = [
        (1.10, 1.101, 1.098, 1.099),
        (1.099, 1.100, 1.0985, 1.0995),  # small candle, does not engulf
    ]
    df = make_candles(rows)
    events = detect_engulfing(df)
    assert len(events) == 0


def test_engulfing_strength_classification():
    # engulfing body much larger than engulfed body -> STRONG
    rows = [
        (1.10, 1.1005, 1.0995, 1.0997),   # small bearish body ~0.0003
        (1.0997, 1.11, 1.099, 1.1090),    # large bullish body ~0.0093, engulfs prior
    ]
    df = make_candles(rows)
    cfg = EngulfingConfig(strong_body_ratio=1.5, normal_body_ratio=1.0)
    events = detect_engulfing(df, config=cfg)
    assert len(events) == 1
    assert events.iloc[0]["strength"] == "STRONG"
