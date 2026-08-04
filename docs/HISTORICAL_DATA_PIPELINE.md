# Historical Data Pipeline

This document describes the institutional historical-data layer added in Task 6.
It extends the existing UTC-first project contract without changing downstream
engines.

## Architecture

```
raw provider files
  -> provider adapter
  -> standardisation
  -> validation
  -> UTC normalisation
  -> Parquet storage
  -> structure / features / strategy / backtest / research
```

The implementation lives in `src/data/historical_pipeline.py`.

## Supported providers

- `DukascopyAdapter`
- `MT5Adapter`
- `CSVAdapter`
- `ParquetAdapter`

Each adapter returns the same internal frame shape.

## Import workflow

1. Read the source file with the provider adapter.
2. Standardise columns and symbol naming.
3. Convert timestamps to UTC.
4. Coerce OHLCV/spread fields to numeric values.
5. Sort chronologically and remove duplicate timestamps.
6. Validate OHLC rules and gap patterns.
7. Persist the processed dataset as Parquet.

## Validation workflow

The validation report captures:

- missing candles
- duplicate timestamps
- out-of-order timestamps
- timezone inconsistencies
- weekend anomalies
- invalid OHLC values
- negative spreads
- large gaps
- corrupted rows
- summary gap and weekend statistics
- overall quality score

## Standard format

Processed datasets use:

- `timestamp` in UTC
- `symbol`
- `timeframe`
- `open`, `high`, `low`, `close`
- `volume`, `spread`, `bid`, `ask` when available
- provenance fields: `source`, `provider`

Local-time views are derived only for reporting.

## Incremental updates

Use `append_processed_dataset(...)` to merge new rows into an existing Parquet
dataset while preventing duplicate imports on `(symbol, timeframe, timestamp)`.

## Known limitations

- Dukascopy-specific compressed download handling is not yet wired to a live
  network fetcher. The adapter layer is ready for it, but the current task
  stays file-based and deterministic.
- Weekend anomaly detection is intentionally conservative and reports rows that
  fall on Saturday or Sunday; provider-specific market-close handling can be
  refined later if needed.

## Example

```python
from src.data.historical_pipeline import build_standard_dataset

dataset = build_standard_dataset(
    "data/raw/EURUSD_M1.csv",
    provider="dukascopy",
    symbol="EURUSD",
    timeframe="M1",
    source_tz="UTC",
)

dataset.data.to_parquet("data/processed/EURUSD_M1.parquet", index=False)
print(dataset.report.summary())
```

