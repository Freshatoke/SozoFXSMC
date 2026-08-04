"""
Historical market data ingestion, validation, and normalization pipeline.

This module extends the existing loader with provider-specific adapters and
institutional-style validation/reporting. The internal output is always a
UTC-normalized, column-stable OHLCV frame suitable for the existing
structure, feature, strategy, backtest, and research engines.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import numpy as np
import pandas as pd

from src.data.providers.histdata import HistDataAdapter
from src.features.storage import save_feature_dataset
from src.utils.timeutils import add_local_views, to_utc

STANDARD_COLUMNS = [
    "timestamp",
    "symbol",
    "timeframe",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "spread",
    "bid",
    "ask",
    "source",
    "provider",
]

REQUIRED_COLUMNS = ["timestamp", "open", "high", "low", "close"]


def normalize_symbol(symbol: str) -> str:
    cleaned = symbol.strip().upper()
    aliases = {
        "EUR/USD": "EURUSD",
        "GBP/USD": "GBPUSD",
        "USD/JPY": "USDJPY",
        "AUD/USD": "AUDUSD",
        "USD/CAD": "USDCAD",
        "USD/CHF": "USDCHF",
        "NZD/USD": "NZDUSD",
        "XAU/USD": "XAUUSD",
        "NAS100": "NAS100",
        "US30": "US30",
        "BTC/USD": "BTCUSD",
    }
    return aliases.get(cleaned, cleaned.replace("/", "").replace(".", ""))


def _empty_standard_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=STANDARD_COLUMNS)


@dataclass
class ValidationReport:
    source_path: str
    provider: str
    symbol: str
    timeframe: str
    rows_read: int = 0
    rows_normalized: int = 0
    total_candles: int = 0
    missing_candles: int = 0
    duplicate_timestamps: int = 0
    out_of_order_timestamps: int = 0
    timezone_inconsistencies: int = 0
    weekend_anomalies: int = 0
    invalid_ohlc_values: int = 0
    negative_spreads: int = 0
    large_gaps: int = 0
    corrupted_rows: int = 0
    gap_statistics: dict = field(default_factory=dict)
    weekend_statistics: dict = field(default_factory=dict)
    timezone_summary: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    missing_timestamp_ranges: list[tuple[str, str]] = field(default_factory=list)
    duplicate_rows_index: list[int] = field(default_factory=list)
    corrupted_rows_index: list[int] = field(default_factory=list)

    @property
    def quality_score(self) -> float:
        penalties = (
            self.missing_candles
            + self.duplicate_timestamps
            + self.out_of_order_timestamps
            + self.timezone_inconsistencies
            + self.weekend_anomalies
            + self.invalid_ohlc_values
            + self.negative_spreads
            + self.large_gaps
            + self.corrupted_rows
        )
        if self.total_candles <= 0:
            return 0.0
        score = 1.0 - min(1.0, penalties / max(1.0, self.total_candles / 10_000.0 + penalties))
        return round(float(score), 4)

    def summary(self) -> str:
        return "\n".join(
            [
                f"Source: {self.source_path}",
                f"Provider: {self.provider}",
                f"Symbol: {self.symbol}",
                f"Timeframe: {self.timeframe}",
                f"Rows read: {self.rows_read}",
                f"Total candles: {self.total_candles}",
                f"Missing candles: {self.missing_candles}",
                f"Duplicate timestamps: {self.duplicate_timestamps}",
                f"Out-of-order timestamps: {self.out_of_order_timestamps}",
                f"Timezone inconsistencies: {self.timezone_inconsistencies}",
                f"Weekend anomalies: {self.weekend_anomalies}",
                f"Invalid OHLC values: {self.invalid_ohlc_values}",
                f"Negative spreads: {self.negative_spreads}",
                f"Large gaps: {self.large_gaps}",
                f"Corrupted rows: {self.corrupted_rows}",
                f"Quality score: {self.quality_score}",
            ]
        )


@dataclass
class HistoricalDataset:
    data: pd.DataFrame
    report: ValidationReport


class DataAdapter(Protocol):
    provider_name: str

    def load(self, path: str | Path, **kwargs) -> pd.DataFrame: ...


def _read_source(path: Path) -> pd.DataFrame:
    if path.suffix.lower() in {".parquet", ".pq"}:
        return pd.read_parquet(path)
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path)
    raise ValueError(f"Unsupported file extension: {path.suffix}")


def _standardize_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    renamed = {c: c.strip().lower() for c in out.columns}
    out = out.rename(columns=renamed)
    if "datetime" in out.columns and "timestamp" not in out.columns:
        out = out.rename(columns={"datetime": "timestamp"})
    if "date" in out.columns and "timestamp" not in out.columns:
        out = out.rename(columns={"date": "timestamp"})
    return out


def _coerce_numeric(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    out = df.copy()
    for col in columns:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    return out


def _validate_ohlc(df: pd.DataFrame) -> pd.Series:
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    return (
        h.isna()
        | l.isna()
        | o.isna()
        | c.isna()
        | (h < l)
        | (h < o)
        | (h < c)
        | (l > o)
        | (l > c)
        | (h <= 0)
        | (l <= 0)
        | (o <= 0)
        | (c <= 0)
    )


def _detect_gaps(ts: pd.Series, expected_interval: str) -> tuple[int, list[tuple[str, str]], dict]:
    if len(ts) < 2:
        return 0, [], {}
    expected = pd.date_range(ts.iloc[0], ts.iloc[-1], freq=expected_interval)
    actual = pd.DatetimeIndex(ts)
    missing = expected.difference(actual)
    if missing.empty:
        return 0, [], {"expected_interval": expected_interval, "gap_count": 0}
    step = pd.Timedelta(expected_interval)
    ranges = []
    start = missing[0]
    prev = missing[0]
    for current in missing[1:]:
        if current - prev != step:
            ranges.append((str(start), str(prev)))
            start = current
        prev = current
    ranges.append((str(start), str(prev)))
    return len(missing), ranges, {"expected_interval": expected_interval, "gap_count": len(ranges)}


def _weekend_stats(ts: pd.Series) -> tuple[int, dict]:
    if len(ts) < 2:
        return 0, {}
    weekend_mask = ts.dt.weekday.isin([5, 6])
    return int(weekend_mask.sum()), {
        "weekend_rows": int(weekend_mask.sum()),
        "weekend_pct": round(float(weekend_mask.mean()), 6),
    }


class DukascopyAdapter:
    provider_name = "dukascopy"

    def load(self, path: str | Path, **kwargs) -> pd.DataFrame:
        return _read_source(Path(path))


class MT5Adapter:
    provider_name = "mt5"

    def load(self, path: str | Path, **kwargs) -> pd.DataFrame:
        return _read_source(Path(path))


class CSVAdapter:
    provider_name = "csv"

    def load(self, path: str | Path, **kwargs) -> pd.DataFrame:
        return _read_source(Path(path))


class ParquetAdapter:
    provider_name = "parquet"

    def load(self, path: str | Path, **kwargs) -> pd.DataFrame:
        return _read_source(Path(path))


ADAPTERS: dict[str, type[DataAdapter]] = {
    "dukascopy": DukascopyAdapter,
    "mt5": MT5Adapter,
    "csv": CSVAdapter,
    "parquet": ParquetAdapter,
    "histdata": HistDataAdapter,
}


def get_adapter(provider: str) -> DataAdapter:
    key = provider.strip().lower()
    if key not in ADAPTERS:
        raise ValueError(f"Unsupported provider: {provider}")
    return ADAPTERS[key]()


def build_standard_dataset(
    path: str | Path,
    *,
    provider: str,
    symbol: str,
    timeframe: str,
    source_tz: str | None = "UTC",
    expected_interval: str = "1min",
    drop_corrupted: bool = True,
    deduplicate: bool = True,
) -> HistoricalDataset:
    adapter = get_adapter(provider)
    raw = adapter.load(path)
    raw = _standardize_columns(raw)
    report = ValidationReport(
        source_path=str(path),
        provider=provider.strip().lower(),
        symbol=normalize_symbol(symbol),
        timeframe=timeframe,
        rows_read=len(raw),
    )

    missing = [c for c in REQUIRED_COLUMNS if c not in raw.columns]
    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    df = raw.copy()
    ts = pd.to_datetime(df["timestamp"], errors="coerce")
    bad_timestamp = ts.isna()
    if bad_timestamp.any():
        report.errors.append("Corrupted timestamp rows detected.")
        report.corrupted_rows += int(bad_timestamp.sum())
        report.corrupted_rows_index = df.index[bad_timestamp].tolist()
        df = df.loc[~bad_timestamp].copy()
        ts = pd.to_datetime(df["timestamp"], errors="raise")

    df["timestamp"] = to_utc(df["timestamp"], source_tz)

    numeric_cols = ["open", "high", "low", "close", "volume", "spread", "bid", "ask"]
    df = _coerce_numeric(df, numeric_cols)
    df["symbol"] = normalize_symbol(symbol)
    df["timeframe"] = timeframe
    df["provider"] = provider.strip().lower()
    df["source"] = str(path)

    before_sort = df["timestamp"].tolist()
    df = df.sort_values("timestamp", kind="mergesort").reset_index(drop=True)
    if before_sort != df["timestamp"].tolist():
        report.out_of_order_timestamps = 1
        report.warnings.append("Timestamps were reordered chronologically.")

    dup_mask = df["timestamp"].duplicated(keep="first")
    report.duplicate_timestamps = int(dup_mask.sum())
    if dup_mask.any():
        report.duplicate_rows_index = df.index[dup_mask].tolist()
        report.warnings.append("Duplicate timestamps were found and the first occurrence was kept.")
        if deduplicate:
            df = df.loc[~dup_mask].reset_index(drop=True)

    invalid_ohlc = _validate_ohlc(df)
    report.invalid_ohlc_values = int(invalid_ohlc.sum())
    if invalid_ohlc.any():
        report.corrupted_rows += int(invalid_ohlc.sum())
        report.errors.append("Invalid OHLC rows detected.")
        if drop_corrupted:
            df = df.loc[~invalid_ohlc].reset_index(drop=True)

    if "spread" in df.columns:
        neg_spread = df["spread"].notna() & (df["spread"] < 0)
        report.negative_spreads = int(neg_spread.sum())
        if neg_spread.any():
            report.warnings.append("Negative spreads detected.")

    report.timezone_summary = {
        "source_tz": source_tz or "embedded",
        "internal_tz": "UTC",
        "rows": len(df),
    }
    report.timezone_inconsistencies = 0 if source_tz or getattr(pd.to_datetime(raw["timestamp"], errors="coerce").dt, "tz", None) is not None else 1
    report.missing_candles, report.missing_timestamp_ranges, report.gap_statistics = _detect_gaps(df["timestamp"], expected_interval)
    report.weekend_anomalies, report.weekend_statistics = _weekend_stats(df["timestamp"])
    report.total_candles = len(df)
    report.rows_normalized = len(df)

    normalized = df[[c for c in STANDARD_COLUMNS if c in df.columns]].copy()
    normalized = add_local_views(normalized)
    return HistoricalDataset(data=normalized.reset_index(drop=True), report=report)


def append_processed_dataset(
    existing_path: str | Path,
    incoming: pd.DataFrame,
    *,
    deduplicate_on: tuple[str, ...] = ("symbol", "timeframe", "timestamp"),
) -> pd.DataFrame:
    existing_path = Path(existing_path)
    if existing_path.exists():
        existing = pd.read_parquet(existing_path)
        combined = pd.concat([existing, incoming], ignore_index=True)
    else:
        combined = incoming.copy()
    combined = combined.sort_values(list(deduplicate_on), kind="mergesort").reset_index(drop=True)
    combined = combined.drop_duplicates(subset=list(deduplicate_on), keep="first").reset_index(drop=True)
    save_feature_dataset(combined, existing_path)
    return combined


def save_processed_dataset(df: pd.DataFrame, path: str | Path) -> None:
    save_feature_dataset(df, path)

