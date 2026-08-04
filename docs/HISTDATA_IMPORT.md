# HistData.com Import (Task 7.3)

Task 7.3 adds a HistData.com ASCII archive importer as a new, permanent
provider in the existing Historical Data Pipeline. It does not change the
structure, feature, strategy, backtest, or research engines, and does not
change the Dukascopy provider (Task 7.1) or the Validation Campaign
(Task 7).

## Architecture

```
scripts/import_histdata.py
  -> src/data/providers/histdata.py     (inspect + parse raw ZIP archives)
  -> src/data/historical_pipeline.py    (ADAPTERS["histdata"] -> build_standard_dataset)
  -> data/raw/EURUSD_M1_histdata.parquet (canonical STANDARD_COLUMNS schema, UTC)
  -> scripts/run_validation_campaign.py (discovers it automatically -- no
                                          "synthetic" in the filename)
```

`HistDataAdapter` (`src/data/providers/histdata.py`) is registered in
`src.data.historical_pipeline.ADAPTERS["histdata"]` exactly like the
existing `dukascopy`/`mt5`/`csv`/`parquet` adapters -- adding it required
one import line and one dict entry in `historical_pipeline.py`; nothing
else in that file changed.

## What HistData.com actually ships (inspected, not assumed)

Each ZIP contains a data CSV and a companion `.txt` gap report (HistData's
own note of gaps it detected -- informational only, never parsed as
data). Two schemas exist:

| Product | Filename contains | Delimiter | Columns |
|---|---|---|---|
| M1 (generic ASCII) | `_M1_` | `;` | `timestamp;open;high;low;close;volume` |
| Tick (generic ASCII) | `_T_` | `,` | `timestamp,bid,ask,volume` |

The 11 archives actually present in `data/imports/histdata/` (EURUSD,
2020 + 2022-2025 annual + 2026 Jan-Jun monthly) are **all M1**, ASCII
encoded, semicolon-delimited, timestamp format `YYYYMMDD HHMMSS`, volume
always `0` (HistData does not provide real tick volume for M1 bars).
`src.data.providers.histdata.inspect_archive` determines this
programmatically per archive (filename pattern, then a delimiter/field-count
fallback if the filename doesn't match either convention) -- it does not
assume the ZIP contents ahead of time.

**Tick files are detected but not yet imported**: `load_histdata_zip`
raises `NotImplementedError` for `_T_`-pattern archives, pointing at
`src.data.providers.dukascopy`'s existing tick-to-M1 aggregation as the
logic a future task should reuse rather than duplicate. This path exists
because Phase 1 requires distinguishing tick from M1, not because any
tick archive is present in this import.

## Timezone: the one detail that would silently corrupt everything if wrong

HistData's generic ASCII timestamps are documented as **Eastern Standard
Time without Daylight Saving adjustments** -- a fixed UTC-5 offset, every
day of the year, including summer. This is represented as the IANA
fixed-offset zone `Etc/GMT+5` (note the inverted sign convention of `Etc`
zones: `Etc/GMT+5` really means UTC-5, and critically has **no DST
transitions ever**).

Using `America/New_York` instead would have been the natural-looking but
WRONG choice: it applies DST, so every summer timestamp would land an
hour off from every winter timestamp relative to the source data's true
convention, silently misaligning every session boundary, every gap
calculation, and every strategy's session-based logic (S5 in particular)
for roughly half the year. `tests/test_histdata_importer.py::test_build_standard_dataset_same_offset_year_round_no_dst_jump`
verifies a summer-dated row shifts by the same fixed 5 hours as a
winter-dated row.

## Extraction safety

`load_histdata_zip` extracts the archive's data file into a
`tempfile.TemporaryDirectory` (auto-cleaned on exit) and opens the
original ZIP read-only throughout -- `tests/test_histdata_importer.py::test_original_zip_is_never_modified`
verifies the archive's bytes and mtime are unchanged after import.

## CLI usage

```bash
python scripts/import_histdata.py \
  --input-dir data/imports/histdata --symbol EURUSD --timeframe M1 \
  --output data/raw/EURUSD_M1_histdata.parquet \
  --report-dir reports/histdata_import
```

Writes the standardized Parquet dataset plus three reports:
- `reports/histdata_import/archive_inspection.json` -- Phase 1 findings, one entry per archive.
- `reports/histdata_import/data_quality_report.md` -- the pipeline's `ValidationReport` plus the market-calendar-aware gap breakdown below.
- `reports/histdata_import/gap_breakdown.json` -- the same breakdown, machine-readable.

Then run the platform exactly as with any other provider:

```bash
python scripts/run_validation_campaign.py --raw-dir data/raw --provider histdata
```

(`discover_dataset_specs` finds `EURUSD_M1_histdata.parquet` automatically
because it matches `EURUSD*.parquet` and doesn't contain "synthetic".)

## Why the quality score alone is misleading for real FX data

`ValidationReport.quality_score` treats every non-present 1-minute
timestamp as a defect. For real FX data spanning years, most "missing"
candles are the market being closed (weekends) — completely normal, not
a defect. On the actual imported dataset this produces a `quality_score`
of `0.0001`, which looks like catastrophic data corruption but is not.

`scripts/import_histdata.py::build_gap_breakdown` classifies every
missing-timestamp range from the SAME underlying `ValidationReport`
(no duplicated gap detection) into:

- **`extended_absence`** (>= 7 days): a fully-missing calendar year, a
  missing product period, etc.
- **`weekend_or_holiday`**: shaped like a normal Friday-close ->
  Sunday-open closure (or Saturday, or Sunday before the ~21:00 UTC
  reopen, or any gap >= 40 hours).
- **`intraweek_gap`**: everything else -- the only category that
  represents a genuine data-quality concern.

This is a pure reporting/interpretation layer added on top of the
existing `ValidationReport` -- it does not change
`ValidationReport.quality_score` or any other pipeline computation.

## Known limitations

- **2021 is entirely absent** from the supplied archives (no
  `HISTDATA_COM_ASCII_EURUSD_M12021.zip` was provided) -- a real,
  ~525,600-minute gap in the source data itself, not an importer defect.
  It is correctly reported under `extended_absence`, and any research run
  over 2020-2026 skips 2021 entirely; this is exactly the kind of thing a
  researcher must know before drawing conclusions from a multi-year
  backtest, which is why it is surfaced explicitly rather than silently
  interpolated or ignored.
- **2026 data ends 2026-06-26** (the source archives don't yet cover late
  June or July 2026) -- reflects HistData's publication lag for the
  current month, not a bug.
- **Tick import is not implemented** (see above) -- only M1 archives are
  supported end-to-end; this matches the actual files supplied for Task
  7.3 and is explicitly, not silently, unsupported for tick archives.
- Only EURUSD was supplied/imported for this task; the adapter itself is
  symbol-agnostic (the symbol is a `build_standard_dataset` parameter,
  not hardcoded).
- **The importer itself is fast** (~65s to parse and standardize all 11
  archives, ~2M rows). The platform's downstream pipeline (structure /
  strategy / backtest / research engines) is NOT yet fast enough to run
  an end-to-end campaign over the full imported dataset in a practical
  amount of time — see `docs/TASK7_3_HISTDATA_VALIDATION_REPORT.md` and
  `docs/TASK7_4_PROMPT.md` for the profiled evidence and the scoped
  optimization task this motivates.
