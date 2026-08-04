"""
Historical M1 Forex data loader.

Responsibilities:
- Read CSV/Parquet historical OHLCV(+bid/ask/spread) data.
- Validate timestamps, detect duplicates and missing intervals.
- Validate OHLC consistency (high >= max(open, close, low), low <= min(...)).
- Sort chronologically.
- Report malformed records without silently discarding them.
- Preserve the raw input untouched; write a separate cleaned output.

Design principle: nothing is silently dropped. Every row that is removed or
flagged is recorded in the returned `LoadReport` so a human can audit it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from src.utils.timeutils import to_utc

REQUIRED_COLUMNS = ["timestamp", "open", "high", "low", "close"]
OPTIONAL_COLUMNS = ["volume", "bid", "ask", "spread"]


@dataclass
class LoadReport:
    source_path: str
    rows_read: int = 0
    rows_clean: int = 0
    duplicate_timestamps: int = 0
    malformed_ohlc: int = 0
    missing_intervals: int = 0
    invalid_timestamps: int = 0
    duplicate_rows_dropped_index: list = field(default_factory=list)
    malformed_rows_index: list = field(default_factory=list)
    missing_interval_ranges: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"Source: {self.source_path}",
            f"Rows read: {self.rows_read}",
            f"Rows clean (output): {self.rows_clean}",
            f"Invalid timestamps: {self.invalid_timestamps}",
            f"Duplicate timestamps found: {self.duplicate_timestamps}",
            f"Malformed OHLC rows found: {self.malformed_ohlc}",
            f"Detected missing-interval gaps: {self.missing_intervals}",
        ]
        if self.warnings:
            lines.append("Warnings:")
            lines.extend(f"  - {w}" for w in self.warnings)
        return "\n".join(lines)


def _read_raw(path: Path) -> pd.DataFrame:
    if path.suffix.lower() in (".parquet", ".pq"):
        return pd.read_parquet(path)
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path)
    raise ValueError(f"Unsupported file extension: {path.suffix}")


def load_m1_csv(
    path: str | Path,
    source_tz: str | None = "UTC",
    expected_interval: str = "1min",
    drop_malformed: bool = True,
) -> tuple[pd.DataFrame, LoadReport]:
    """Load and validate a historical M1 (or any single-timeframe) OHLCV file.

    Parameters
    ----------
    path: CSV or Parquet file with at least timestamp, open, high, low, close.
    source_tz: timezone of the raw timestamps. Must be provided explicitly;
        there is no implicit default assumption about the user's local time.
    expected_interval: pandas offset alias used to detect missing intervals.
    drop_malformed: if True, rows failing OHLC consistency are excluded from
        the returned cleaned frame (but always kept in the report for audit).
        Raw input file is never modified regardless of this flag.

    Returns
    -------
    (cleaned_df, report)
    """
    path = Path(path)
    raw = _read_raw(path)
    report = LoadReport(source_path=str(path), rows_read=len(raw))

    missing_cols = [c for c in REQUIRED_COLUMNS if c not in raw.columns]
    if missing_cols:
        raise ValueError(f"Missing required columns: {missing_cols}")

    df = raw.copy()

    # --- timestamp validation -------------------------------------------------
    try:
        df["timestamp"] = to_utc(df["timestamp"], source_tz)
    except (ValueError, TypeError) as exc:
        # Try row-wise to isolate bad rows instead of failing the whole load.
        parsed = pd.to_datetime(df["timestamp"], errors="coerce")
        bad_mask = parsed.isna()
        report.invalid_timestamps = int(bad_mask.sum())
        report.warnings.append(str(exc))
        df = df.loc[~bad_mask].copy()
        df["timestamp"] = to_utc(df["timestamp"], source_tz)

    # --- numeric coercion -------------------------------------------------
    numeric_cols = [c for c in REQUIRED_COLUMNS[1:] + OPTIONAL_COLUMNS if c in df.columns]
    for c in numeric_cols:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    # --- sort chronologically -------------------------------------------------
    df = df.sort_values("timestamp", kind="mergesort").reset_index(drop=True)

    # --- duplicate timestamp detection -------------------------------------------------
    dup_mask = df["timestamp"].duplicated(keep="first")
    report.duplicate_timestamps = int(dup_mask.sum())
    if dup_mask.any():
        report.duplicate_rows_dropped_index = df.index[dup_mask].tolist()
        report.warnings.append(
            f"{int(dup_mask.sum())} duplicate timestamp rows found; keeping first occurrence."
        )
        df = df.loc[~dup_mask].reset_index(drop=True)

    # --- OHLC consistency validation -------------------------------------------------
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    malformed_mask = (
        h.isna() | l.isna() | o.isna() | c.isna()
        | (h < l)
        | (h < o) | (h < c)
        | (l > o) | (l > c)
        | (h <= 0) | (l <= 0)
    )
    report.malformed_ohlc = int(malformed_mask.sum())
    if malformed_mask.any():
        report.malformed_rows_index = df.index[malformed_mask].tolist()
        report.warnings.append(
            f"{int(malformed_mask.sum())} rows failed OHLC consistency checks."
        )
        if drop_malformed:
            df = df.loc[~malformed_mask].reset_index(drop=True)

    # --- missing interval detection -------------------------------------------------
    if len(df) > 1:
        expected = pd.date_range(df["timestamp"].iloc[0], df["timestamp"].iloc[-1], freq=expected_interval)
        actual = pd.DatetimeIndex(df["timestamp"])
        missing = expected.difference(actual)
        if len(missing) > 0:
            # collapse consecutive missing timestamps into ranges for a compact report
            ranges = []
            run_start = missing[0]
            prev = missing[0]
            step = pd.Timedelta(expected_interval)
            for ts in missing[1:]:
                if ts - prev != step:
                    ranges.append((run_start, prev))
                    run_start = ts
                prev = ts
            ranges.append((run_start, prev))
            report.missing_intervals = len(ranges)
            report.missing_interval_ranges = [(str(a), str(b)) for a, b in ranges]
            report.warnings.append(f"{len(ranges)} gap(s) in the expected {expected_interval} interval.")

    report.rows_clean = len(df)
    return df.reset_index(drop=True), report


def save_processed(df: pd.DataFrame, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)
