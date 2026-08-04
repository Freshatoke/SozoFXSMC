from config.settings import DisplacementConfig
from src.features.displacement import detect_displacement
from tests.helpers import make_candles


def _quiet_rows(n, base=1.1000, step=0.0002):
    rows = []
    price = base
    for _ in range(n):
        o = price
        c = price + step
        h = c + 0.0001
        l = o - 0.0001
        rows.append((o, h, l, c))
        price = c
    return rows


def test_bullish_displacement_detected():
    rows = _quiet_rows(20)
    last_close = rows[-1][3]
    # one large impulsive bullish candle: body far larger than recent average / ATR
    rows.append((last_close, last_close + 0.0210, last_close - 0.0005, last_close + 0.0200))
    df = make_candles(rows)
    events = detect_displacement(df, config=DisplacementConfig())
    assert len(events) == 1
    ev = events.iloc[0]
    assert ev["direction"] == "bullish"
    assert ev["start_index"] == 20
    assert ev["reasons"][0]["conditions_met"] >= 2


def test_bearish_displacement_detected():
    rows = _quiet_rows(20)
    last_close = rows[-1][3]
    rows.append((last_close, last_close + 0.0005, last_close - 0.0210, last_close - 0.0200))
    df = make_candles(rows)
    events = detect_displacement(df, config=DisplacementConfig())
    assert len(events) == 1
    assert events.iloc[0]["direction"] == "bearish"


def test_no_displacement_in_quiet_market():
    rows = _quiet_rows(30)
    df = make_candles(rows)
    events = detect_displacement(df, config=DisplacementConfig())
    assert len(events) == 0
