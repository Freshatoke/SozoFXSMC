# AI Trading Journal & Daily Intelligence Reports (Task 11.2)

## What "AI" means here

`src/live/journal.py` is a deterministic, rule-based **analysis engine over recorded operational data** — not a language model call, not machine learning, and not anything that trains or adapts. Every number in a report traces back to an activity record written by the live platform. "AI Observations" means automated pattern-detection over real data with a fixed, auditable rule set (`generate_observations`, `detect_drift`, `generate_recommendations` in `src/live/journal.py`), not an emergent or opaque process. This is a deliberate reading of the task's own rule: *"Never claim the platform 'learned' something unless it is explicitly comparing historical evidence and identifying statistically supported patterns."* Every comparison this module makes is against real, previously-saved daily reports — when there isn't enough history, it says so explicitly rather than inventing a conclusion (see "Honest gaps" below).

**The journal is an analyst, not a trader.** It never touches `src/strategies/`, `src/decision_engine/ios.py`, `src/research/itqs.py`, or any config the decision engine reads. It only reads activity records and writes reports/logs.

## Architecture

```
Throughout the trading day (every 5 min via telegram-scan.yml,
or continuously via a running LiveOrchestrator):

  DailyActivityRecorder.record_scan/record_decision/
  record_trade_opened/record_trade_closed/record_feed_error
                    │
                    ▼
  data/live/journal/activity/<YYYY-MM-DD>.jsonl   (append-only, one day's raw events)

At 22:00 Nigerian time (daily-report.yml):

  generate_daily_report()  -- Phase 1: reads the day's activity, computes every metric
  generate_observations()  -- Phase 2: rule-based statements from today's numbers only
  compare_historical()     -- Phase 3: vs prior week/month, from saved past reports
  detect_drift()           -- Phase 4: distribution/frequency shift vs a real baseline
  generate_recommendations() -- Phase 5: operational flags only, never auto-applied
  render_report_markdown() -- Telegram-formatted report
                    │
                    ├──▶ Telegram (TelegramNotifier)
                    ├──▶ data/live/journal/reports/<date>.json   (persisted, for tomorrow's comparison)
                    └──▶ append_learning_log()  -- Phase 6:
                           data/live/journal/learning_log.jsonl   (source of truth)
                           docs/journal/LEARNING_LOG.md            (human-readable, cumulative)
```

## Report fields (Phase 1)

| Field | Source |
|---|---|
| Markets scanned, candles processed | `scan` activity records |
| Signals detected, approved/rejected opportunities | `decision` activity records (one per `LiveDecisionEngine` verdict) |
| Open/closed paper trades, wins, losses, win rate, expectancy, profit factor | `trade_opened`/`trade_closed` activity records — **only present when a paper broker actually ran** (see "Honest gaps" below) |
| Highest IOS, Highest ITQS | max across today's `decision` records |
| Best strategy, best symbol | ranked by realized PnL if any trades closed today, else by approved-opportunity count (the report says which basis was used) |
| Most common rejection reason | `reasons_against` strings bucketed into stable categories (`categorize_rejection_reason`) — matched against the actual literal messages `portfolio_allocation.py`/`risk_layer.py` produce, not guessed |

## Honest gaps (deliberately not glossed over)

- **The GitHub Actions scan-only deployment (`telegram-scan.yml`) does not run a paper broker.** Reports generated purely from that deployment's activity will correctly show `open/closed_paper_trades`, `wins`, `losses`, `win_rate`, `expectancy`, `profit_factor` as `null`/"not applicable", with an explicit note (`no_paper_broker_data: true`, and a matching observation sentence) — never a misleading `0`. These fields only populate once a `LiveOrchestrator` (with its `PaperBroker`) is actually run and feeding the same activity log (see `src/live/orchestrator.py`'s `journal_recorder` wiring).
- **Historical comparison and drift detection need real history to exist.** `compare_historical` requires at least 1 prior saved daily report for a "week" comparison and 3 for "month" (`MIN_DAYS_FOR_HISTORICAL_COMPARISON`); `detect_drift` requires at least 7 (`MIN_DAYS_FOR_DRIFT_BASELINE`). Below those thresholds, the report states "insufficient history" and makes no comparison/drift claim — this is the module's own enforcement of the task's "do not invent conclusions" rule, not a bug.
- **`candles_processed` under the scan-only deployment overcounts.** Each 5-minute scan re-polls its whole lookback window from scratch (no persisted cursor across the disposable Actions VMs — see `docs/GITHUB_ACTIONS_SETUP_GUIDE.md`), so the same candles get counted again on the next scan. This is an honest reflection of actual redundant work performed, not trade activity, and is a known, already-documented consequence of the temporary deployment's statelessness.

## Running it manually

```bash
python scripts/generate_daily_report.py --date 2026-08-05          # sends to Telegram
python scripts/generate_daily_report.py --no-telegram              # print/save only, no send
```

## Scheduling

`.github/workflows/daily-report.yml` runs on `cron: "0 21 * * *"` — 21:00 UTC, which is 22:00 Africa/Lagos (WAT) year-round since Nigeria does not observe daylight saving time. It depends on `telegram-scan.yml` having accumulated that day's activity file via its own commits throughout the day.

## The Learning Log (Phase 6)

`docs/journal/LEARNING_LOG.md` is generated, append-only, and grows by one dated section per day (key observations, detected anomalies, recommendations) — see it directly for the platform's actual operational history once the schedule has run for a few days. `data/live/journal/learning_log.jsonl` is the same information in machine-readable form, which `compare_historical`/`detect_drift` never read directly (they read the per-day `reports/<date>.json` files instead) — the JSONL log is a human/audit record, not the statistical input.
