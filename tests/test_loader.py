import pandas as pd

from src.data.loader import load_m1_csv
from tests.helpers import make_candles


def _write_csv(tmp_path, df):
    path = tmp_path / "input.csv"
    df_out = df.copy()
    df_out["timestamp"] = df_out["timestamp"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    df_out.to_csv(path, index=False)
    return path


def test_malformed_ohlc_is_flagged_and_dropped(tmp_path):
    rows = [
        (1.00, 1.02, 0.99, 1.01),
        (1.01, 1.03, 1.05, 1.02),  # malformed: low > high
        (1.02, 1.04, 1.00, 1.03),
    ]
    df = make_candles(rows)
    path = _write_csv(tmp_path, df)

    cleaned, report = load_m1_csv(path, source_tz="UTC", drop_malformed=True)

    assert report.malformed_ohlc == 1
    assert len(cleaned) == 2
    assert report.rows_read == 3
    assert report.rows_clean == 2


def test_duplicate_timestamp_handling(tmp_path):
    rows = [
        (1.00, 1.02, 0.99, 1.01),
        (1.01, 1.03, 1.00, 1.02),
        (1.02, 1.04, 1.00, 1.03),
    ]
    df = make_candles(rows)
    # duplicate the first timestamp onto the second row
    df.loc[1, "timestamp"] = df.loc[0, "timestamp"]
    path = _write_csv(tmp_path, df)

    cleaned, report = load_m1_csv(path, source_tz="UTC")

    assert report.duplicate_timestamps == 1
    assert len(cleaned) == 2
    assert cleaned["timestamp"].is_unique


def test_missing_interval_detected(tmp_path):
    rows = [
        (1.00, 1.02, 0.99, 1.01),
        (1.01, 1.03, 1.00, 1.02),
        (1.02, 1.04, 1.00, 1.03),
    ]
    df = make_candles(rows, freq="1min")
    df = df.drop(index=1).reset_index(drop=True)  # remove the middle minute -> gap
    path = _write_csv(tmp_path, df)

    cleaned, report = load_m1_csv(path, source_tz="UTC")

    assert report.missing_intervals >= 1


def test_sorted_chronologically(tmp_path):
    rows = [
        (1.00, 1.02, 0.99, 1.01),
        (1.01, 1.03, 1.00, 1.02),
        (1.02, 1.04, 1.00, 1.03),
    ]
    df = make_candles(rows, freq="1min")
    shuffled = df.iloc[[2, 0, 1]].reset_index(drop=True)
    path = _write_csv(tmp_path, shuffled)

    cleaned, report = load_m1_csv(path, source_tz="UTC")

    assert list(cleaned["timestamp"]) == sorted(cleaned["timestamp"])
