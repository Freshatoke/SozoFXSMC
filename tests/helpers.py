import numpy as np
import pandas as pd


def make_candles(ohlc_rows, freq="1min", start="2024-01-01 00:00:00"):
    """ohlc_rows: list of (open, high, low, close) tuples. Returns a UTC-indexed OHLC df."""
    ts = pd.date_range(start, periods=len(ohlc_rows), freq=freq, tz="UTC")
    df = pd.DataFrame(ohlc_rows, columns=["open", "high", "low", "close"])
    df.insert(0, "timestamp", ts)
    return df


def make_multi_day_m1(num_days=10, start="2024-01-01 00:00:00", seed=7, base=1.1000):
    """Synthetic multi-day M1 OHLCV with an injected trend so BOS/CHoCH,
    weekend gaps, and sessions all have something real to detect. `start`
    should fall on a Monday for a clean weekend-gap test window. Fully
    deterministic given the same seed."""
    n = num_days * 1440
    rng = np.random.default_rng(seed)
    ts = pd.date_range(start, periods=n, freq="1min", tz="UTC")
    returns = rng.normal(0, 0.0003, n)
    trend = np.sin(np.linspace(0, 6 * np.pi, n)) * 0.002
    close = base + np.cumsum(returns + np.diff(np.concatenate([[0], trend])))
    open_ = np.concatenate([[base], close[:-1]])
    high = np.maximum(open_, close) + np.abs(rng.normal(0, 0.0002, n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0, 0.0002, n))
    df = pd.DataFrame({"timestamp": ts, "open": open_, "high": high, "low": low, "close": close})

    # Drop the weekend (Saturday + most of Sunday) so a real gap exists,
    # then inject an explicit reopen gap so S1 always has something to find.
    dow = df["timestamp"].dt.weekday
    keep = ~(dow.isin([5]) | ((dow == 6) & (df["timestamp"].dt.hour < 22)))
    df = df[keep].reset_index(drop=True)

    friday_mask = df["timestamp"].dt.weekday == 4
    if friday_mask.any():
        last_friday_idx = df.index[friday_mask][-1]
        if last_friday_idx + 1 < len(df):
            gap_size = 0.0080
            shift = df.loc[last_friday_idx, "close"] + gap_size - df.loc[last_friday_idx + 1, "open"]
            df.loc[last_friday_idx + 1:, ["open", "high", "low", "close"]] += shift

    return df.reset_index(drop=True)
