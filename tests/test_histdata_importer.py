"""
Task 7.3 tests: HistData.com ASCII importer.

Uses small synthetic ZIP fixtures built in-memory (never touching the
real archives in data/imports/histdata/) so these tests are fast,
deterministic, and don't depend on the real dataset being present.
"""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pandas as pd
import pytest

from src.data.historical_pipeline import build_standard_dataset, get_adapter
from src.data.providers.histdata import (
    HISTDATA_TIMEZONE,
    HistDataAdapter,
    inspect_all_archives,
    inspect_archive,
    load_histdata_zip,
)
from scripts.import_histdata import build_gap_breakdown, _classify_gap


M1_CSV = (
    "20240101 170000;1.100000;1.100500;1.099500;1.100200;0\n"
    "20240101 170100;1.100200;1.100800;1.100100;1.100600;0\n"
    "20240101 170200;1.100600;1.100900;1.100400;1.100700;0\n"
)

TICK_CSV = (
    "20240101 170000123,1.100000,1.100100,0\n"
    "20240101 170000456,1.100050,1.100150,0\n"
)


def _make_m1_zip(path: Path, symbol: str = "EURUSD", period: str = "2024") -> Path:
    zip_path = path / f"HISTDATA_COM_ASCII_{symbol}_M1{period}.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(f"DAT_ASCII_{symbol}_M1_{period}.csv", M1_CSV)
        zf.writestr(f"DAT_ASCII_{symbol}_M1_{period}.txt", "HistData.com (c) 2012\nNo gaps.\n")
    return zip_path


def _make_tick_zip(path: Path, symbol: str = "EURUSD", period: str = "202401") -> Path:
    zip_path = path / f"HISTDATA_COM_ASCII_{symbol}_T_{period}.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(f"DAT_ASCII_{symbol}_T_{period}.csv", TICK_CSV)
    return zip_path


def _make_corrupt_zip(path: Path) -> Path:
    zip_path = path / "corrupt.zip"
    zip_path.write_bytes(b"not actually a zip file")
    return zip_path


# ---------------------------------------------------------------------------
# Archive inspection (Phase 1)
# ---------------------------------------------------------------------------


def test_inspect_archive_detects_m1_schema(tmp_path):
    zip_path = _make_m1_zip(tmp_path)
    inspection = inspect_archive(zip_path)

    assert inspection.error is None
    assert inspection.schema_type == "m1_ohlcv"
    assert inspection.delimiter == ";"
    assert inspection.encoding == "ascii"
    assert inspection.timestamp_format == "%Y%m%d %H%M%S"
    assert inspection.timezone == HISTDATA_TIMEZONE
    assert inspection.columns == ["timestamp", "open", "high", "low", "close", "volume"]
    assert inspection.volume_present is False
    assert inspection.data_file.endswith(".csv")
    assert inspection.report_file.endswith(".txt")


def test_inspect_archive_detects_tick_schema(tmp_path):
    zip_path = _make_tick_zip(tmp_path)
    inspection = inspect_archive(zip_path)

    assert inspection.error is None
    assert inspection.schema_type == "tick"
    assert inspection.delimiter == ","
    assert inspection.columns == ["timestamp", "bid", "ask", "volume"]


def test_inspect_archive_handles_corrupt_zip_gracefully(tmp_path):
    zip_path = _make_corrupt_zip(tmp_path)
    inspection = inspect_archive(zip_path)
    assert inspection.error is not None
    assert inspection.schema_type == "unknown"


def test_inspect_all_archives_scans_directory(tmp_path):
    _make_m1_zip(tmp_path, period="2024")
    _make_m1_zip(tmp_path, period="2025")
    inspections = inspect_all_archives(tmp_path)
    assert len(inspections) == 2
    assert all(i.schema_type == "m1_ohlcv" for i in inspections)


# ---------------------------------------------------------------------------
# Parsing + extraction safety
# ---------------------------------------------------------------------------


def test_load_histdata_zip_parses_m1_values(tmp_path):
    zip_path = _make_m1_zip(tmp_path)
    df = load_histdata_zip(zip_path)

    assert list(df.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert len(df) == 3
    assert df["timestamp"].iloc[0] == pd.Timestamp("2024-01-01 17:00:00")
    assert df["open"].iloc[0] == pytest.approx(1.100000)
    assert df["close"].iloc[-1] == pytest.approx(1.100700)
    assert (df["volume"] == 0).all()


def test_load_histdata_zip_rejects_tick_schema(tmp_path):
    zip_path = _make_tick_zip(tmp_path)
    with pytest.raises(NotImplementedError):
        load_histdata_zip(zip_path)


def test_load_histdata_zip_rejects_unknown_schema(tmp_path):
    zip_path = _make_corrupt_zip(tmp_path)
    with pytest.raises(ValueError):
        load_histdata_zip(zip_path)


def test_original_zip_is_never_modified(tmp_path):
    zip_path = _make_m1_zip(tmp_path)
    before_bytes = zip_path.read_bytes()
    before_hash = hashlib.sha256(before_bytes).hexdigest()
    before_mtime = zip_path.stat().st_mtime

    load_histdata_zip(zip_path)

    after_hash = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    assert after_hash == before_hash
    assert zip_path.stat().st_mtime == before_mtime


# ---------------------------------------------------------------------------
# Adapter: directory aggregation, single-file, passthrough
# ---------------------------------------------------------------------------


def test_adapter_aggregates_directory_of_zips_chronologically(tmp_path):
    _make_m1_zip(tmp_path, period="2025")  # intentionally written "later" period first
    _make_m1_zip(tmp_path, period="2024")
    adapter = HistDataAdapter()
    df = adapter.load(tmp_path)

    assert len(df) == 6  # 3 rows per archive x 2 archives
    assert list(df["timestamp"]) == sorted(df["timestamp"])


def test_adapter_loads_single_zip_file(tmp_path):
    zip_path = _make_m1_zip(tmp_path)
    adapter = HistDataAdapter()
    df = adapter.load(zip_path)
    assert len(df) == 3


def test_adapter_passthrough_for_already_standardized_parquet(tmp_path):
    standardized = pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01", periods=3, freq="1min", tz="UTC"),
        "open": [1.1, 1.1, 1.1], "high": [1.1, 1.1, 1.1], "low": [1.1, 1.1, 1.1], "close": [1.1, 1.1, 1.1],
    })
    path = tmp_path / "already_standard.parquet"
    standardized.to_parquet(path, index=False)

    adapter = HistDataAdapter()
    df = adapter.load(path)
    pd.testing.assert_frame_equal(df, standardized)


def test_adapter_registered_in_pipeline():
    adapter = get_adapter("histdata")
    assert adapter.provider_name == "histdata"


def test_adapter_directory_with_no_zips_raises(tmp_path):
    adapter = HistDataAdapter()
    with pytest.raises(ValueError):
        adapter.load(tmp_path)


# ---------------------------------------------------------------------------
# End-to-end: build_standard_dataset with correct fixed-offset timezone
# ---------------------------------------------------------------------------


def test_build_standard_dataset_converts_est_to_utc_correctly(tmp_path):
    """HistData's 17:00:00 (EST, fixed UTC-5, no DST) must become 22:00 UTC
    -- NOT 21:00 or 22:00-shifted-by-DST, which is what would happen if
    `America/New_York` (a DST-aware zone) were used instead of the fixed
    `Etc/GMT+5` offset."""
    _make_m1_zip(tmp_path)
    dataset = build_standard_dataset(
        tmp_path, provider="histdata", symbol="EURUSD", timeframe="M1",
        source_tz=HISTDATA_TIMEZONE, expected_interval="1min",
    )
    first_ts = dataset.data["timestamp"].iloc[0]
    assert first_ts == pd.Timestamp("2024-01-01 22:00:00", tz="UTC")


def test_build_standard_dataset_same_offset_year_round_no_dst_jump(tmp_path):
    """A summer-dated HistData timestamp must shift by the SAME fixed 5
    hours as a winter one -- proving no DST adjustment leaks in."""
    summer_csv = "20240701 170000;1.100000;1.100500;1.099500;1.100200;0\n"
    zip_path = tmp_path / "HISTDATA_COM_ASCII_EURUSD_M12024b.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("DAT_ASCII_EURUSD_M1_2024b.csv", summer_csv)

    dataset = build_standard_dataset(
        tmp_path, provider="histdata", symbol="EURUSD", timeframe="M1",
        source_tz=HISTDATA_TIMEZONE, expected_interval="1min",
    )
    ts = dataset.data["timestamp"].iloc[0]
    assert ts.hour == 22  # 17:00 + fixed 5h, same as winter -- no DST shift to 21:00


# ---------------------------------------------------------------------------
# Gap classification (scripts/import_histdata.py)
# ---------------------------------------------------------------------------


def test_classify_gap_weekend_vs_intraweek_vs_extended():
    friday_close = pd.Timestamp("2024-01-05 22:00:00", tz="UTC")
    sunday_open = pd.Timestamp("2024-01-07 21:59:00", tz="UTC")
    assert _classify_gap(friday_close, sunday_open) == "weekend_or_holiday"

    small_start = pd.Timestamp("2024-01-10 13:00:00", tz="UTC")
    small_end = pd.Timestamp("2024-01-10 13:05:00", tz="UTC")
    assert _classify_gap(small_start, small_end) == "intraweek_gap"

    long_start = pd.Timestamp("2021-01-01", tz="UTC")
    long_end = pd.Timestamp("2021-12-31", tz="UTC")
    assert _classify_gap(long_start, long_end) == "extended_absence"


def test_build_gap_breakdown_counts_categories():
    ranges = [
        ("2024-01-05 22:00:00+00:00", "2024-01-07 21:59:00+00:00"),  # weekend
        ("2024-01-10 13:00:00+00:00", "2024-01-10 13:05:00+00:00"),  # intraweek
        ("2021-01-01 00:00:00+00:00", "2021-12-31 00:00:00+00:00"),  # extended
    ]
    breakdown = build_gap_breakdown(ranges)
    assert breakdown["range_counts"]["weekend_or_holiday"] == 1
    assert breakdown["range_counts"]["intraweek_gap"] == 1
    assert breakdown["range_counts"]["extended_absence"] == 1
    assert breakdown["num_intraweek_gaps"] == 1
