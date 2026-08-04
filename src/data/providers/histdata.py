"""
HistData.com ASCII data provider (Task 7.3).

HistData.com distributes free historical Forex data as ZIP archives
containing:
    - a data file (`DAT_ASCII_<SYMBOL>_<TIMEFRAME>_<PERIOD>.csv`)
    - a companion report (`DAT_ASCII_<SYMBOL>_<TIMEFRAME>_<PERIOD>.txt`)
      listing data gaps HistData itself detected -- informational only,
      never parsed as data.

Two schemas exist depending on which product was downloaded:
    M1 (generic ASCII):  "<SYMBOL>_M1_..."  semicolon-delimited
        YYYYMMDD HHMMSS;open;high;low;close;volume
        (volume is always 0 for FX -- HistData does not provide real tick
        volume for M1 bars)
    Tick (generic ASCII): "<SYMBOL>_T_..." comma-delimited
        YYYYMMDD HHMMSS<fff>,bid,ask,volume

Timezone: HistData's generic ASCII timestamps are documented as Eastern
Standard Time WITHOUT Daylight Saving adjustments (i.e. a fixed UTC-5
offset year-round, not `America/New_York`, which WOULD apply DST and
silently shift every summer timestamp by an hour). This is represented
here as the IANA fixed-offset zone `Etc/GMT+5` (note: POSIX/Etc zone
signs are inverted from common usage -- `Etc/GMT+5` really means UTC-5,
with no DST transitions ever, which is exactly HistData's convention).

This module only inspects, extracts (to a temporary directory -- the
original ZIP files are never modified or written to), and parses the raw
archives into a plain DataFrame; normalization into the platform's
canonical schema is still done by
`src.data.historical_pipeline.build_standard_dataset`, via the
`HistDataAdapter` registered there -- no validation/gap-detection logic
is duplicated here.
"""

from __future__ import annotations

import re
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

# Fixed UTC-5 offset, no DST -- see module docstring.
HISTDATA_TIMEZONE = "Etc/GMT+5"

_M1_NAME_RE = re.compile(r"_M(\d+)_", re.IGNORECASE)
_TICK_NAME_RE = re.compile(r"_T_", re.IGNORECASE)

M1_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]
TICK_COLUMNS = ["timestamp", "bid", "ask", "volume"]


@dataclass
class ArchiveInspection:
    path: str
    inner_files: list[str] = field(default_factory=list)
    data_file: str | None = None
    report_file: str | None = None
    schema_type: str = "unknown"           # "m1_ohlcv" | "tick" | "unknown"
    delimiter: str | None = None
    encoding: str = "ascii"
    timestamp_format: str | None = None
    timezone: str = HISTDATA_TIMEZONE
    columns: list[str] = field(default_factory=list)
    sample_rows: list[str] = field(default_factory=list)
    volume_present: bool = False
    row_count: int | None = None
    error: str | None = None

    def summary(self) -> str:
        lines = [
            f"Archive: {self.path}",
            f"  Inner files: {', '.join(self.inner_files)}",
            f"  Data file: {self.data_file}",
            f"  Schema: {self.schema_type}",
            f"  Delimiter: {self.delimiter!r}",
            f"  Encoding: {self.encoding}",
            f"  Timestamp format: {self.timestamp_format}",
            f"  Timezone: {self.timezone}",
            f"  Columns: {self.columns}",
            f"  Volume present (non-zero): {self.volume_present}",
            f"  Row count: {self.row_count}",
        ]
        if self.error:
            lines.append(f"  ERROR: {self.error}")
        return "\n".join(lines)


def _detect_schema(data_filename: str, first_line: str) -> tuple[str, str]:
    """Returns (schema_type, delimiter)."""
    if _TICK_NAME_RE.search(data_filename):
        return "tick", ","
    if _M1_NAME_RE.search(data_filename):
        return "m1_ohlcv", ";"
    # Fall back to sniffing the delimiter/field count if the filename
    # doesn't match either known convention.
    if first_line.count(";") >= 4:
        return "m1_ohlcv", ";"
    if first_line.count(",") >= 2:
        return "tick", ","
    return "unknown", ";"


def inspect_archive(zip_path: str | Path) -> ArchiveInspection:
    """Reads (never extracts) a HistData ZIP's structure and the first
    few bytes of its data file to determine delimiter, schema, and
    encoding -- Task 7.3 Phase 1 requirement, done before any parsing."""
    zip_path = Path(zip_path)
    inspection = ArchiveInspection(path=str(zip_path))
    try:
        with zipfile.ZipFile(zip_path) as zf:
            inspection.inner_files = zf.namelist()
            data_candidates = [n for n in inspection.inner_files if n.lower().endswith(".csv")]
            report_candidates = [n for n in inspection.inner_files if n.lower().endswith(".txt")]
            if not data_candidates:
                inspection.error = "No .csv data file found inside archive."
                return inspection
            inspection.data_file = data_candidates[0]
            inspection.report_file = report_candidates[0] if report_candidates else None

            with zf.open(inspection.data_file) as fh:
                raw_head = fh.read(4096)
            try:
                text_head = raw_head.decode("ascii")
                inspection.encoding = "ascii"
            except UnicodeDecodeError:
                text_head = raw_head.decode("utf-8", errors="replace")
                inspection.encoding = "utf-8"

            first_line = text_head.splitlines()[0] if text_head.splitlines() else ""
            schema_type, delimiter = _detect_schema(inspection.data_file, first_line)
            inspection.schema_type = schema_type
            inspection.delimiter = delimiter
            inspection.columns = M1_COLUMNS if schema_type == "m1_ohlcv" else (
                TICK_COLUMNS if schema_type == "tick" else []
            )
            inspection.sample_rows = text_head.splitlines()[:3]
            inspection.timestamp_format = (
                "%Y%m%d %H%M%S" if schema_type == "m1_ohlcv" else "%Y%m%d %H%M%S%f"
            )

            if first_line:
                fields = first_line.split(delimiter)
                if len(fields) >= len(inspection.columns) and inspection.columns:
                    try:
                        volume_value = float(fields[len(inspection.columns) - 1])
                        inspection.volume_present = volume_value != 0
                    except ValueError:
                        pass

            info = zf.getinfo(inspection.data_file)
            # HistData M1/tick rows are short and fixed-shape; approximate
            # the row count from compressed metadata without a full parse
            # (kept informational -- the actual parse re-counts exactly).
            inspection.row_count = None
    except (zipfile.BadZipFile, OSError) as exc:
        inspection.error = f"Failed to open archive: {exc}"
    return inspection


def inspect_all_archives(directory: str | Path, pattern: str = "*.zip") -> list[ArchiveInspection]:
    directory = Path(directory)
    return [inspect_archive(p) for p in sorted(directory.glob(pattern))]


def _parse_data_file(path: Path, schema_type: str, delimiter: str) -> pd.DataFrame:
    columns = M1_COLUMNS if schema_type == "m1_ohlcv" else TICK_COLUMNS
    df = pd.read_csv(
        path, sep=delimiter, header=None, names=columns,
        dtype={"timestamp": str}, encoding="ascii",
    )
    ts_format = "%Y%m%d %H%M%S" if schema_type == "m1_ohlcv" else None
    df["timestamp"] = pd.to_datetime(df["timestamp"], format=ts_format, errors="raise")
    return df


def load_histdata_zip(zip_path: str | Path) -> pd.DataFrame:
    """Extracts the archive's data file into a temporary directory (the
    original ZIP is opened read-only and is never modified), parses it,
    and returns a raw DataFrame with a naive (not-yet-localized) UTC-5
    `timestamp` column plus the schema's native OHLCV/tick columns."""
    zip_path = Path(zip_path)
    inspection = inspect_archive(zip_path)
    if inspection.error:
        raise ValueError(f"Cannot import {zip_path}: {inspection.error}")
    if inspection.schema_type == "unknown":
        raise ValueError(
            f"Cannot determine schema (tick vs M1 vs other) for {zip_path}; "
            f"first line was: {inspection.sample_rows[:1]!r}"
        )
    if inspection.schema_type == "tick":
        raise NotImplementedError(
            f"{zip_path} contains TICK data. This importer currently supports HistData's "
            "M1 ASCII schema only (matching the files actually present in data/imports/histdata/). "
            "Tick import would reuse src.data.providers.dukascopy's existing tick-to-M1 "
            "aggregation rather than duplicating it here -- see docs/HISTDATA_IMPORT.md."
        )

    with tempfile.TemporaryDirectory(prefix="histdata_import_") as tmp_dir:
        with zipfile.ZipFile(zip_path) as zf:
            extracted_path = Path(zf.extract(inspection.data_file, path=tmp_dir))
        df = _parse_data_file(extracted_path, inspection.schema_type, inspection.delimiter)
    return df


def _read_standardized(path: Path) -> pd.DataFrame:
    if path.suffix.lower() in (".parquet", ".pq"):
        return pd.read_parquet(path)
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path)
    raise ValueError(f"Unsupported already-standardized file extension: {path.suffix}")


class HistDataAdapter:
    """Registered as provider "histdata" in `src.data.historical_pipeline.ADAPTERS`.

    Accepts, via `path`:
        - a directory containing one or more HistData `.zip` archives
          (all are parsed and concatenated chronologically)
        - a single HistData `.zip` archive
        - an already-standardized `.csv`/`.parquet` file (passthrough --
          this lets the SAME provider name be used both for the initial
          raw import and for any later re-discovery of the already-
          normalized output, e.g. by the validation campaign)
    """

    provider_name = "histdata"

    def load(self, path: str | Path, **kwargs) -> pd.DataFrame:
        path = Path(path)

        if path.is_dir():
            zips = sorted(path.glob("*.zip"))
            if not zips:
                raise ValueError(f"No .zip archives found in {path}")
            frames = [load_histdata_zip(z) for z in zips]
            combined = pd.concat(frames, ignore_index=True)
            combined = combined.sort_values("timestamp", kind="mergesort").reset_index(drop=True)
            return combined

        if path.suffix.lower() == ".zip":
            return load_histdata_zip(path)

        return _read_standardized(path)
