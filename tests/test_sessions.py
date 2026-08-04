import pandas as pd

from src.features.sessions import compute_sessions
from config.settings import SessionConfig


def _make_day_candles(date_str, n=1440):
    ts = pd.date_range(f"{date_str} 00:00:00", periods=n, freq="1min", tz="UTC")
    price = 1.1000
    rows = []
    for _ in range(n):
        rows.append((price, price + 0.0002, price - 0.0002, price + 0.0001))
        price += 0.00001
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    df.insert(0, "timestamp", ts)
    return df


def test_london_session_dst_shift():
    # 2024-01-15: GMT (UTC+0), London session 08:00-16:30 local -> 08:00-16:30 UTC
    winter = _make_day_candles("2024-01-15")
    sessions_winter = compute_sessions(winter)
    london_winter = sessions_winter[sessions_winter.session_name == "london"].iloc[0]
    assert london_winter["start_utc"].hour == 8

    # 2024-07-15: BST (UTC+1), London session 08:00-16:30 local -> 07:00-15:30 UTC
    summer = _make_day_candles("2024-07-15")
    sessions_summer = compute_sessions(summer)
    london_summer = sessions_summer[sessions_summer.session_name == "london"].iloc[0]
    assert london_summer["start_utc"].hour == 7


def test_session_high_low_computed():
    df = _make_day_candles("2024-01-15")
    sessions = compute_sessions(df)
    london = sessions[sessions.session_name == "london"].iloc[0]
    window = df[(df["timestamp"] >= london["start_utc"]) & (df["timestamp"] < london["end_utc"])]
    assert london["high"] == window["high"].max()
    assert london["low"] == window["low"].min()
    assert london["num_candles"] == len(window)
