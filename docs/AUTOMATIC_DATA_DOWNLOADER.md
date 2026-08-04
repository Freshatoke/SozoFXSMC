# Automatic Data Downloader

Task 7.1 adds automated Dukascopy historical data acquisition. It feeds the
existing Historical Data Pipeline and Validation Campaign without changing the
research, backtesting, strategy, feature, or structure engines.

## Architecture

```
scripts/download_history.py
  -> src/data/providers/dukascopy.py
  -> data/raw/dukascopy cache
  -> normalized tick CSV
  -> optional M1 candle CSV
  -> Historical Data Pipeline validation
  -> processed Parquet output
  -> Validation Campaign
```

Dukascopy tick data is served as one compressed `.bi5` file per hour. The CLI
accepts normal date ranges and the provider module handles the 24 hourly files
for each requested day.

## CLI Usage

Download three years of EURUSD ticks:

```bash
python scripts/download_history.py \
  --symbol EURUSD \
  --start 2022-01-01 \
  --end 2024-12-31 \
  --type tick
```

Download Gold:

```bash
python scripts/download_history.py \
  --symbol XAUUSD \
  --start 2018-01-01 \
  --end 2024-12-31 \
  --type tick
```

Download multiple symbols:

```bash
python scripts/download_history.py \
  --symbols EURUSD GBPUSD XAUUSD \
  --start 2022-01-01 \
  --end 2024-12-31
```

Build M1 candles from ticks:

```bash
python scripts/download_history.py \
  --symbol EURUSD \
  --start 2022-01-01 \
  --end 2024-12-31 \
  --type tick \
  --build-m1
```

Resume the previous request:

```bash
python scripts/download_history.py --resume
```

Resume an explicit range:

```bash
python scripts/download_history.py \
  --symbol EURUSD \
  --start 2022-01-01 \
  --end 2024-12-31 \
  --resume
```

## Cache

Downloaded files are stored under:

```text
data/raw/dukascopy/
```

Raw hourly files are cached by symbol/year/month/day/hour. Valid cached files
are reused and are not downloaded again.

## Resume Mode

The downloader maintains:

```text
data/raw/dukascopy/download_resume_state.json
```

After one explicit run, `--resume` reloads that request and skips days already
marked valid in the metadata catalogue.

## Metadata Catalogue

The catalogue is stored at:

```text
data/raw/dukascopy/metadata_catalogue.json
```

It records:

- symbol
- provider
- date range
- number of days
- number of records
- download date
- validation status
- quality score
- raw, tick, and M1 file paths

## Integrity Verification

Each cached hourly file is checked before reuse:

- file exists
- file size is greater than zero
- file is readable
- `.bi5` payload decompresses
- tick record length is valid
- timestamps are monotonic

Corrupted cache files are deleted and downloaded again.

## Tick Normalization

Ticks are normalized to:

- `timestamp` in UTC
- `symbol`
- `bid`
- `ask`
- `spread`
- `bid_volume`
- `ask_volume`
- `provider`

Normalized daily tick CSV files are written under each symbol's cache folder.

## Tick Aggregation

`--build-m1` converts ticks into M1 candles using:

- open: first mid price in the minute
- high: maximum mid price in the minute
- low: minimum mid price in the minute
- close: last mid price in the minute
- volume: tick count
- bid/ask/spread: latest or average values for reporting

The resulting M1 file is passed through the Historical Data Pipeline.

## Configuration

Provider defaults live in `config/settings.py` as
`DukascopyDownloadConfig`.

Configurable values:

- worker count
- retry count
- timeout
- cache location
- provider URL
- output format

CLI flags override the defaults for a run.

## Troubleshooting

- If `--resume` has no saved state, run one explicit command with symbol and
  dates first.
- If Parquet export fails, install project dependencies with
  `pip install -r requirements.txt`; the project requires `pyarrow`.
- If many days are marked missing, verify the symbol is available on
  Dukascopy and that the requested dates include active trading days.
- If downloads are rate-limited, reduce `--workers` and increase retries.

