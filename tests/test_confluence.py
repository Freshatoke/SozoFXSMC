from src.features.confluence import build_confluence_snapshot
from tests.helpers import make_candles
from tests.test_displacement import _quiet_rows


def test_confluence_snapshot_reflects_bullish_structure_and_ob():
    rows = _quiet_rows(20)
    last_close = rows[-1][3]
    bearish_ob_candle = (last_close, last_close + 0.0002, last_close - 0.0005, last_close - 0.0003)
    rows.append(bearish_ob_candle)
    disp_start = bearish_ob_candle[3]
    rows.append((disp_start, disp_start + 0.0210, disp_start - 0.0005, disp_start + 0.0200))
    df = make_candles(rows)

    snapshot = build_confluence_snapshot(df, as_of_index=len(df) - 1, symbol="TEST", timeframe="M1")

    assert snapshot["timestamp"] == df["timestamp"].iloc[-1]
    assert snapshot["symbol"] == "TEST"
    assert isinstance(snapshot["active_bullish_ob_count"], int)
    assert snapshot["active_bullish_ob_count"] >= 1
    assert snapshot["fresh_bullish_ob"] in (True, False)


def test_confluence_snapshot_is_queryable_independently():
    rows = _quiet_rows(10)
    df = make_candles(rows)
    snapshot = build_confluence_snapshot(df, as_of_index=5, symbol="TEST", timeframe="M1")
    expected_keys = {
        "structure_state", "last_structure_event_type", "active_bullish_ob_count",
        "active_bearish_ob_count", "fresh_bullish_ob", "active_bullish_fvg_count",
        "pdh_swept", "pdl_swept", "asian_low_swept", "strong_engulfing_recent",
        "open_weekend_gap",
    }
    assert expected_keys.issubset(snapshot.keys())
    assert snapshot["structure_state"] == "UNKNOWN"
