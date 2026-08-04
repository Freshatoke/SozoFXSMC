"""Run the Task 7 real-market validation campaign."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.research.validation_campaign import (
    DEFAULT_SYMBOLS,
    discover_dataset_specs,
    run_validation_campaign,
)


def _write_blocked_report(out_dir: Path, raw_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = "\n".join(
        [
            "# Real Market Validation Summary",
            "",
            "## Status",
            "",
            "Blocked: no real historical datasets were found in the input directory.",
            "",
            "## Required Inputs",
            "",
            "Place one CSV or Parquet M1 file per symbol in the raw data directory.",
            "Synthetic files are intentionally ignored for this campaign.",
            "",
            "Expected symbols:",
            "",
            ", ".join(DEFAULT_SYMBOLS),
            "",
            f"Raw data directory checked: `{raw_dir}`",
        ]
    )
    (out_dir / "validation_summary.md").write_text(summary, encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", default="data/raw")
    parser.add_argument("--processed-dir", default="data/processed/historical")
    parser.add_argument("--out-dir", default="reports/validation_campaign")
    parser.add_argument("--provider", default="dukascopy")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    raw_dir = Path(args.raw_dir)
    out_dir = Path(args.out_dir)
    specs = discover_dataset_specs(raw_dir, provider=args.provider)
    if not specs:
        _write_blocked_report(out_dir, raw_dir)
        print(f"No real historical datasets found. Wrote {out_dir / 'validation_summary.md'}")
        return 0

    result = run_validation_campaign(
        specs,
        out_dir=out_dir,
        processed_dir=args.processed_dir,
    )
    print(f"Analysed {len(result.datasets)} datasets and generated {len(result.trades)} trades.")
    print(f"Reports written to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
