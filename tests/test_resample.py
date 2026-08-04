import pandas as pd

from src.data.resample import resample_ohlc
from tests.helpers import make_candles


def test_m1_to_m5_aggregation():
    rows = [(1 + i * 0.01, 1.02 + i * 0.01, 0.99 + i * 0.01, 1.01 + i * 0.01) for i in range(10)]
    df = make_candles(rows, freq="1min")
    out = resample_ohlc(df, "5min")
    assert len(out) == 2

    bucket0 = df.iloc[0:5]
    bucket1 = df.iloc[5:10]

    assert out.iloc[0]["open"] == bucket0["open"].iloc[0]
    assert out.iloc[0]["close"] == bucket0["close"].iloc[-1]
    assert out.iloc[0]["high"] == bucket0["high"].max()
    assert out.iloc[0]["low"] == bucket0["low"].min()

    assert out.iloc[1]["open"] == bucket1["open"].iloc[0]
    assert out.iloc[1]["close"] == bucket1["close"].iloc[-1]
    assert out.iloc[1]["high"] == bucket1["high"].max()
    assert out.iloc[1]["low"] == bucket1["low"].min()


def test_m1_to_m15_aggregation():
    rows = [(1 + i * 0.01, 1.02 + i * 0.01, 0.99 + i * 0.01, 1.01 + i * 0.01) for i in range(30)]
    df = make_candles(rows, freq="1min")
    out = resample_ohlc(df, "15min")
    assert len(out) == 2

    bucket0 = df.iloc[0:15]
    bucket1 = df.iloc[15:30]

    assert out.iloc[0]["open"] == bucket0["open"].iloc[0]
    assert out.iloc[0]["close"] == bucket0["close"].iloc[-1]
    assert out.iloc[0]["high"] == bucket0["high"].max()
    assert out.iloc[0]["low"] == bucket0["low"].min()
    assert out.iloc[1]["open"] == bucket1["open"].iloc[0]


def test_resample_bar_close_time_is_after_last_constituent_candle():
    rows = [(1 + i * 0.01, 1.02 + i * 0.01, 0.99 + i * 0.01, 1.01 + i * 0.01) for i in range(5)]
    df = make_candles(rows, freq="1min")
    out = resample_ohlc(df, "5min")
    last_constituent_ts = df["timestamp"].iloc[-1]
    assert out.iloc[0]["bar_close_time"] > last_constituent_ts
