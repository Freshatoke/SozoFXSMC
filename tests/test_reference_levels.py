import pandas as pd

from src.features.reference_levels import compute_reference_levels, compute_weekend_gaps


def _daily_candles(start_date, num_days, base=1.10):
    rows = []
    price = base
    for d in range(num_days):
        day_ts = pd.Timestamp(start_date, tz="UTC") + pd.Timedelta(days=d)
        for m in range(0, 1440, 60):  # hourly candles for compact test data
            ts = day_ts + pd.Timedelta(minutes=m)
            o = price
            c = price + 0.0005 * (1 if d % 2 == 0 else -1)
            h = max(o, c) + 0.001 + d * 0.001
            l = min(o, c) - 0.001
            rows.append((ts, o, h, l, c))
            price = c
    return pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close"])


def test_pdh_pdl_calculation():
    df = _daily_candles("2024-01-15", 3)  # Mon, Tue, Wed
    ref = compute_reference_levels(df)
    pdh = ref[ref.level_type == "PDH"].reset_index(drop=True)
    pdl = ref[ref.level_type == "PDL"].reset_index(drop=True)
    assert len(pdh) >= 1
    assert len(pdl) >= 1

    day0 = df[df["timestamp"].dt.date == df["timestamp"].dt.date.iloc[0]]
    first_pdh = pdh.iloc[0]
    assert first_pdh["value"] == day0["high"].max()
    # PDH for day0 must only become available at/after day0 ends (day1 start)
    assert first_pdh["available_from"] > day0["timestamp"].iloc[-1]


def test_weekend_gap_calculation():
    # Friday 2024-01-12 -> reopen Sunday 2024-01-14 (weekend gap)
    friday = pd.date_range("2024-01-12 00:00:00", "2024-01-12 21:59:00", freq="1min", tz="UTC")
    sunday = pd.date_range("2024-01-14 22:00:00", "2024-01-15 02:00:00", freq="1min", tz="UTC")
    ts = friday.append(sunday)

    price = 1.1000
    rows = []
    for i, t in enumerate(ts):
        if t < friday[-1] + pd.Timedelta(minutes=1):
            c = price + 0.00001
        else:
            c = price  # keep flat after the gap for a simple, predictable test
        o = price
        h = max(o, c) + 0.0001
        l = min(o, c) - 0.0001
        rows.append((t, o, h, l, c))
        price = c
    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close"])

    # introduce the actual gap: reopen candle opens higher than friday close
    reopen_idx = len(friday)
    df.loc[reopen_idx, "open"] = df.loc[reopen_idx - 1, "close"] + 0.0050
    df.loc[reopen_idx, "high"] = df.loc[reopen_idx, "open"] + 0.0002
    df.loc[reopen_idx, "low"] = df.loc[reopen_idx, "open"] - 0.0002
    df.loc[reopen_idx, "close"] = df.loc[reopen_idx, "open"] + 0.0001

    gaps = compute_weekend_gaps(df)
    assert len(gaps) == 1
    gap = gaps.iloc[0]
    assert gap["gap_direction"] == "up"
    assert gap["gap_size"] > 0
    assert gap["state"] in ("OPEN", "PARTIALLY_FILLED", "FILLED")
