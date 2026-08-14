"""
Task 11.2/11.3 — Daily Intelligence Report generator (v2).

Run once per trading day, after the day's activity is complete (the
GitHub Actions schedule fires this at 22:00 Nigerian time / 21:00 UTC,
see .github/workflows/daily-report.yml). Reads today's recorded activity
(data/live/journal/activity/<date>.jsonl, accumulated throughout the day
by many separate telegram-scan.yml runs -- each committing its own
contribution back to the repo, see Task 11.3's fix to that workflow),
computes the full v2 report, sends it to Telegram, persists it for future
historical comparisons, appends one entry to the cumulative learning log,
and also writes the human-readable operational journal for the day.
Nothing here modifies strategies, IOS, ITQS, the decision engine, or the
paper broker -- this script only reads recorded activity and reports.

Usage:
    python scripts/generate_daily_report.py [--date YYYY-MM-DD] [--no-telegram]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.live.journal import (
    nigeria_reporting_date, generate_daily_report, save_daily_report, generate_observations,
    compare_historical, detect_drift, generate_recommendations, render_daily_report_v2_markdown,
    render_operational_journal, append_learning_log,
)
from src.live.notifications import TelegramNotifier, NotConfiguredError

MONITORED_SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCHF", "USDCAD", "NZDUSD"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None,
                         help="YYYY-MM-DD (Nigeria-local). Defaults to nigeria_reporting_date() -- "
                              "'today' unless it's currently before 06:00 Lagos time, in which case "
                              "'yesterday' (handles GitHub's scheduled-trigger delay; see journal.py).")
    parser.add_argument("--activity-dir", default="data/live/journal/activity")
    parser.add_argument("--reports-dir", default="data/live/journal/reports")
    parser.add_argument("--journal-dir", default="data/live/journal/operational")
    parser.add_argument("--learning-log-jsonl", default="data/live/journal/learning_log.jsonl")
    parser.add_argument("--learning-log-md", default="docs/journal/LEARNING_LOG.md")
    parser.add_argument("--no-telegram", action="store_true", help="Compute and print/save the report without sending to Telegram.")
    args = parser.parse_args()

    date = args.date or nigeria_reporting_date()

    report = generate_daily_report(date, activity_dir=args.activity_dir)
    observations = generate_observations(report)
    historical = compare_historical(report, journal_dir=args.reports_dir)
    drift = detect_drift(report, journal_dir=args.reports_dir)
    recommendations = generate_recommendations(report, drift)
    markdown = render_daily_report_v2_markdown(report, observations, historical, drift, recommendations,
                                                monitored_symbols=MONITORED_SYMBOLS)

    print(markdown)

    save_daily_report(report, journal_dir=args.reports_dir)
    append_learning_log(report, observations, drift, recommendations,
                         jsonl_path=args.learning_log_jsonl, markdown_path=args.learning_log_md)

    journal_path = Path(args.journal_dir) / f"{date}.txt"
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal_path.write_text(render_operational_journal(date, activity_dir=args.activity_dir), encoding="utf-8")
    print(f"\nOperational journal written to {journal_path}")

    if not args.no_telegram:
        try:
            TelegramNotifier().notify(f"Daily Intelligence Report - {date}", markdown)
            print(f"\nSent to Telegram.")
        except NotConfiguredError as exc:
            print(f"\nTelegram not configured, report saved locally only: {exc}", file=sys.stderr)
        except Exception as exc:
            print(f"\nTelegram send failed, report saved locally only: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
