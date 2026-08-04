"""
Task 7.3 -- Import a HistData.com ASCII archive directory into the
platform's Historical Data Pipeline and produce:

    1. An Import Report (Phase 1): what `inspect_archive` found in every
       ZIP -- schema, delimiter, encoding, timestamp format, timezone.
    2. A Data Quality Report (Phase 2): the existing pipeline's
       `ValidationReport` (duplicates, malformed rows, chronological
       ordering, raw gap count) PLUS a market-calendar-aware breakdown of
       *why* candles are missing -- weekend closures and the dataset's
       known extended-absence periods (e.g. a missing calendar year) are
       expected/benign and reported separately from genuine intra-week
       gaps, since lumping them together (as the underlying
       `ValidationReport.quality_score` does, by design -- it has no
       market-calendar awareness) makes a perfectly normal FX dataset
       look like it is 99.99% broken.

This script does not change the underlying `ValidationReport`/adapter
logic (see src/data/historical_pipeline.py, src/data/providers/histdata.py)
-- it only adds an interpretive layer on top for reporting purposes.

Usage:
    python scripts/import_histdata.py \
        --input-dir data/imports/histdata --symbol EURUSD --timeframe M1 \
        --output data/raw/EURUSD_M1_histdata.parquet \
        --report-dir reports/histdata_import
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from src.data.historical_pipeline import build_standard_dataset, save_processed_dataset
from src.data.providers.histdata import HISTDATA_TIMEZONE, inspect_all_archives


def _classify_gap(start: pd.Timestamp, end: pd.Timestamp) -> str:
    """Classifies one missing-timestamp range from the ValidationReport
    as an expected market closure or a genuine data gap, using only the
    range's own start/end (no re-scan of the full candle series)."""
    duration = end - start
    if duration >= pd.Timedelta(days=7):
        return "extended_absence"   # e.g. the 2021 calendar year entirely missing from this dataset
    is_weekend_shaped = (
        start.weekday() == 5  # Saturday
        or (start.weekday() == 6 and start.hour < 21)  # Sunday before the ~21:00 UTC reopen
        or duration >= pd.Timedelta(hours=40)  # a normal Fri-close -> Sun-open closure
    )
    if is_weekend_shaped:
        return "weekend_or_holiday"
    return "intraweek_gap"


def build_gap_breakdown(missing_ranges: list[tuple[str, str]]) -> dict:
    counts = {"extended_absence": 0, "weekend_or_holiday": 0, "intraweek_gap": 0}
    total_minutes = {"extended_absence": 0.0, "weekend_or_holiday": 0.0, "intraweek_gap": 0.0}
    intraweek_examples = []

    for start_str, end_str in missing_ranges:
        start, end = pd.Timestamp(start_str), pd.Timestamp(end_str)
        category = _classify_gap(start, end)
        counts[category] += 1
        minutes = (end - start).total_seconds() / 60.0 + 1  # inclusive of both ends (1-min candles)
        total_minutes[category] += minutes
        if category == "intraweek_gap":
            intraweek_examples.append({"start": start_str, "end": end_str, "minutes": round(minutes, 1)})

    intraweek_examples.sort(key=lambda r: r["minutes"], reverse=True)
    return {
        "range_counts": counts,
        "missing_minutes_by_category": {k: round(v, 1) for k, v in total_minutes.items()},
        "largest_intraweek_gaps": intraweek_examples[:20],
        "num_intraweek_gaps": len(intraweek_examples),
    }


def write_import_report(report_dir: Path, inspections, dataset_report, gap_breakdown: dict) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)

    inspection_payload = [
        {
            "path": i.path, "inner_files": i.inner_files, "data_file": i.data_file,
            "schema_type": i.schema_type, "delimiter": i.delimiter, "encoding": i.encoding,
            "timestamp_format": i.timestamp_format, "timezone": i.timezone,
            "columns": i.columns, "volume_present": i.volume_present, "error": i.error,
        }
        for i in inspections
    ]
    (report_dir / "archive_inspection.json").write_text(json.dumps(inspection_payload, indent=2))

    lines = [
        "# HistData Import Report", "",
        "## Phase 1: Archive Inspection", "",
        f"- Archives inspected: {len(inspections)}",
        f"- Schema detected: {sorted({i.schema_type for i in inspections})}",
        f"- Delimiter: {sorted({i.delimiter for i in inspections})}",
        f"- Encoding: {sorted({i.encoding for i in inspections})}",
        f"- Timestamp format: {sorted({i.timestamp_format for i in inspections})}",
        f"- Timezone (source): `{HISTDATA_TIMEZONE}` (fixed UTC-5, no DST -- HistData's documented convention)",
        f"- Archives with errors: {sum(1 for i in inspections if i.error)}",
        "",
        "## Phase 2: Data Quality Report", "",
        dataset_report.summary().replace("\n", "\n"),
        "",
        "### Market-calendar-aware gap breakdown",
        "",
        "The raw `missing_candles`/`quality_score` above treat every non-present "
        "1-minute timestamp as a defect, including weekends (when FX markets are "
        "closed) and this dataset's fully-absent calendar year. The breakdown below "
        "separates those from genuine gaps:",
        "",
        f"- Extended absences (>= 7 days, e.g. a missing calendar year): "
        f"{gap_breakdown['range_counts']['extended_absence']} range(s), "
        f"{gap_breakdown['missing_minutes_by_category']['extended_absence']:.0f} minutes",
        f"- Weekend/holiday closures: {gap_breakdown['range_counts']['weekend_or_holiday']} range(s), "
        f"{gap_breakdown['missing_minutes_by_category']['weekend_or_holiday']:.0f} minutes",
        f"- Genuine intra-week gaps: {gap_breakdown['range_counts']['intraweek_gap']} range(s), "
        f"{gap_breakdown['missing_minutes_by_category']['intraweek_gap']:.0f} minutes",
        "",
        "Largest genuine intra-week gaps (top 20):",
        "",
    ]
    for g in gap_breakdown["largest_intraweek_gaps"]:
        lines.append(f"- {g['start']} -> {g['end']} ({g['minutes']:.0f} min)")
    if not gap_breakdown["largest_intraweek_gaps"]:
        lines.append("- none")

    (report_dir / "data_quality_report.md").write_text("\n".join(lines))
    (report_dir / "gap_breakdown.json").write_text(json.dumps(gap_breakdown, indent=2))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", default="data/imports/histdata")
    parser.add_argument("--symbol", default="EURUSD")
    parser.add_argument("--timeframe", default="M1")
    parser.add_argument("--output", default="data/raw/EURUSD_M1_histdata.parquet")
    parser.add_argument("--report-dir", default="reports/histdata_import")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_dir = Path(args.input_dir)
    report_dir = Path(args.report_dir)

    print(f"Inspecting archives in {input_dir} ...")
    inspections = inspect_all_archives(input_dir)
    for insp in inspections:
        print(insp.summary())
        print()
        if insp.error:
            print(f"WARNING: {insp.path} could not be inspected: {insp.error}")

    unknown = [i for i in inspections if i.schema_type == "unknown"]
    if unknown:
        print(f"Refusing to import: {len(unknown)} archive(s) have an unrecognized schema.")
        for i in unknown:
            print(f"  - {i.path}")
        return 1

    print("\nBuilding standardized dataset (this parses every archive; may take ~1 minute for multi-year data) ...")
    dataset = build_standard_dataset(
        input_dir, provider="histdata", symbol=args.symbol, timeframe=args.timeframe,
        source_tz=HISTDATA_TIMEZONE, expected_interval="1min",
    )
    print(dataset.report.summary())

    gap_breakdown = build_gap_breakdown(dataset.report.missing_timestamp_ranges)
    write_import_report(report_dir, inspections, dataset.report, gap_breakdown)

    save_processed_dataset(dataset.data, args.output)
    print(f"\nSaved standardized dataset to {args.output} ({len(dataset.data)} rows).")
    print(f"Reports written to {report_dir}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
