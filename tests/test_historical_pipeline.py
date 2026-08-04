import pandas as pd

from src.data.historical_pipeline import (
    append_processed_dataset,
    build_standard_dataset,
    normalize_symbol,
    save_processed_dataset,
)
from tests.helpers import make_candles


def _write_csv(tmp_path, df):
    path = tmp_path / "input.csv"
    df_out = df.copy()
    if df_out["timestamp"].dt.tz is not None:
        df_out["timestamp"] = df_out["timestamp"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    df_out.to_csv(path, index=False)
    return path


def test_symbol_normalization():
    assert normalize_symbol("eur/usd") == "EURUSD"
    assert normalize_symbol("XAU/USD") == "XAUUSD"
    assert normalize_symbol("NAS100") == "NAS100"


def test_build_standard_dataset_normalizes_and_reports(tmp_path):
    rows = [
        (1.00, 1.02, 0.99, 1.01),
        (1.01, 1.04, 1.00, 1.03),
        (1.03, 1.05, 1.02, 1.04),
    ]
    df = make_candles(rows)
    df["spread"] = [0.0001, 0.0002, 0.0001]
    df["volume"] = [10, 11, 12]
    path = _write_csv(tmp_path, df)

    dataset = build_standard_dataset(
        path,
        provider="csv",
        symbol="eur/usd",
        timeframe="M1",
        source_tz="UTC",
        expected_interval="1min",
    )

    assert list(dataset.data["symbol"].unique()) == ["EURUSD"]
    assert list(dataset.data["timeframe"].unique()) == ["M1"]
    assert dataset.report.total_candles == 3
    assert dataset.report.quality_score == 1.0
    assert "timestamp_new_york" in dataset.data.columns


def test_duplicate_and_gap_detection(tmp_path):
    # 4 candles at t0..t3 (1min apart), t0 duplicated (appended, not
    # overwritten -- overwriting index1's timestamp onto index0's would
    # collapse both rows onto the same instant and leave only one unique
    # timestamp, making a gap impossible to observe at all), and t2
    # dropped so a real gap survives dedup between the remaining t1/t3.
    rows = [
        (1.00, 1.02, 0.99, 1.01),
        (1.01, 1.03, 1.00, 1.02),
        (1.02, 1.04, 1.00, 1.03),
        (1.03, 1.05, 1.02, 1.04),
    ]
    df = make_candles(rows, freq="1min")
    duplicate_row = df.loc[[0]]
    df = pd.concat([df, duplicate_row], ignore_index=True)
    df = df.drop(index=2).reset_index(drop=True)
    path = _write_csv(tmp_path, df)

    dataset = build_standard_dataset(
        path,
        provider="csv",
        symbol="EURUSD",
        timeframe="M1",
        source_tz="UTC",
    )

    assert dataset.report.duplicate_timestamps == 1
    assert dataset.report.missing_candles >= 1


def test_invalid_ohlc_is_flagged(tmp_path):
    rows = [
        (1.00, 1.02, 0.99, 1.01),
        (1.01, 1.03, 1.05, 1.02),
        (1.02, 1.04, 1.00, 1.03),
    ]
    df = make_candles(rows)
    path = _write_csv(tmp_path, df)

    dataset = build_standard_dataset(
        path,
        provider="csv",
        symbol="EURUSD",
        timeframe="M1",
        source_tz="UTC",
    )

    assert dataset.report.invalid_ohlc_values == 1
    assert len(dataset.data) == 2


def test_incremental_append_deduplicates(tmp_path):
    base = pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=2, freq="1min", tz="UTC"),
            "symbol": ["EURUSD", "EURUSD"],
            "timeframe": ["M1", "M1"],
            "open": [1.0, 1.1],
            "high": [1.1, 1.2],
            "low": [0.9, 1.0],
            "close": [1.05, 1.15],
        }
    )
    path = tmp_path / "processed.parquet"
    save_processed_dataset(base, path)

    new = pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01 00:01:00", periods=2, freq="1min", tz="UTC"),
            "symbol": ["EURUSD", "EURUSD"],
            "timeframe": ["M1", "M1"],
            "open": [1.1, 1.2],
            "high": [1.2, 1.3],
            "low": [1.0, 1.1],
            "close": [1.15, 1.25],
        }
    )

    combined = append_processed_dataset(path, new)
    assert len(combined) == 3
    assert combined["timestamp"].is_unique

