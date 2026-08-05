"""
Task 11.2 Phase 1/6 — Daily Intelligence Report generator.

Run once per trading day, after the day's activity is complete (the
GitHub Actions schedule fires this at 22:00 Nigerian time / 21:00 UTC,
see .github/workflows/daily-report.yml). Reads today's recorded activity
(data/live/journal/activity/<date>.jsonl, written throughout the day by
scripts/telegram_scan_and_notify.py and/or a running LiveOrchestrator),
computes the full report, sends it to Telegram, persists it for future
historical comparisons, and appends one entry to the cumulative learning
log. Nothing here modifies strategies, IOS, ITQS, or the decision engine
-- this script only reads and reports.

Usage:
    python scripts/generate_daily_report.py [--date YYYY-MM-DD] [--no-telegram]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.live.journal import (
    nigeria_today, generate_daily_report, save_daily_report, generate_observations,
    compare_historical, detect_drift, generate_recommendations, render_report_markdown,
    append_learning_log,
)
from src.live.notifications import TelegramNotifier, NotConfiguredError


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None, help="YYYY-MM-DD (Nigeria-local). Defaults to today.")
    parser.add_argument("--activity-dir", default="data/live/journal/activity")
    parser.add_argument("--reports-dir", default="data/live/journal/reports")
    parser.add_argument("--learning-log-jsonl", default="data/live/journal/learning_log.jsonl")
    parser.add_argument("--learning-log-md", default="docs/journal/LEARNING_LOG.md")
    parser.add_argument("--no-telegram", action="store_true", help="Compute and print/save the report without sending to Telegram.")
    args = parser.parse_args()

    date = args.date or nigeria_today()

    report = generate_daily_report(date, activity_dir=args.activity_dir)
    observations = generate_observations(report)
    historical = compare_historical(report, journal_dir=args.reports_dir)
    drift = detect_drift(report, journal_dir=args.reports_dir)
    recommendations = generate_recommendations(report, drift)
    markdown = render_report_markdown(report, observations, historical, drift, recommendations)

    print(markdown)

    save_daily_report(report, journal_dir=args.reports_dir)
    append_learning_log(report, observations, drift, recommendations,
                         jsonl_path=args.learning_log_jsonl, markdown_path=args.learning_log_md)

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
