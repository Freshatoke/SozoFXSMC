# Operational State Architecture (Task 11.3)

## The problem this solves

`telegram-scan.yml` runs every 5 minutes, each time on a **fresh, disposable GitHub Actions VM** with no memory of any prior run. Before Task 11.3, `scripts/telegram_scan_and_notify.py` wrote its activity records to `data/live/journal/activity/<date>.jsonl` on that disposable VM's local disk — and the workflow's commit step only ever `git add`ed `data/live/notified_opportunities.json` (the dedupe state file). The activity file was **never committed**, so it vanished the instant each job ended. `scripts/generate_daily_report.py`, running later in a completely separate checkout, read an activity file that had never been pushed — hence "Markets scanned: none" despite scans genuinely succeeding. Task 11.3's root fix is one line: `telegram-scan.yml` now also `git add data/live/journal/activity/`.

## Why JSON files committed to the repo (not SQLite/Parquet/Artifacts/Releases)

Phase 9 asked for "the simplest reliable architecture, no external infrastructure." All the listed options were considered:

| Option | Why not chosen |
|---|---|
| SQLite as a workflow artifact | Artifacts have a retention window (default 90 days) and aren't trivially diffable/mergeable across concurrent runs; a binary file also can't be `git diff`'d for review. |
| Parquet operational logs | Binary format, same merge/diff problem, and adds a dependency (`pyarrow`) to a script that otherwise only needs the stdlib + pandas already in `requirements.txt`. |
| GitHub Artifacts | Explicitly for build outputs, not long-lived state; not designed to accumulate across scheduled runs the way this needs. |
| GitHub Releases | Meant for versioned release assets, not a rapidly-appended operational log; would need its own API client and doesn't version well with `git log`. |
| **JSON/JSONL files committed to the repo** | **Chosen.** Plain text, diffable, mergeable (JSONL's one-record-per-line shape makes `git merge`/rebase conflicts rare and resolvable), needs zero extra dependencies, and `git` itself IS the persistence + replication + history mechanism — exactly what Phase 9 asked for ("simplest reliable architecture... no external infrastructure"). |

## The three persisted files

| File | Written by | Committed by | Purpose |
|---|---|---|---|
| `data/live/journal/activity/<date>.jsonl` | Every `telegram_scan_and_notify.py` run (and `LiveOrchestrator`, when running continuously) | `telegram-scan.yml`, every 5 minutes | Raw, append-only event log: one line per scan/decision/trade/feed-error/cycle-summary. Source of truth for the day. |
| `data/live/journal/reports/<date>.json` | `generate_daily_report.py`, once per day | `daily-report.yml` | The fully computed daily report (Phase 1/3/4 aggregation) -- what tomorrow's historical comparison/drift detection read, so they never have to re-aggregate raw activity. |
| `data/live/journal/learning_log.jsonl` + `docs/journal/LEARNING_LOG.md` | `generate_daily_report.py`, once per day | `daily-report.yml` | Phase 6's cumulative journal -- observations/anomalies/recommendations, human-readable and machine-readable. |

## How concurrent writers avoid losing data

Two independent workflows (`telegram-scan.yml` every 5 minutes, `daily-report.yml` once daily) both commit to `main`. Two safeguards:

1. **`concurrency: group: telegram-scan`** in `telegram-scan.yml` serializes overlapping scan runs against each other (a slow run and its successor never race).
2. **`git pull --rebase origin main` immediately before every `git push`**, in both workflows' commit steps. If the OTHER workflow pushed in between this job's checkout and its commit, the rebase picks up that intervening commit before pushing — since every write only ever APPENDS a new line to a JSONL file or adds a new day's `.json` file, a rebase here essentially never has a real content conflict (the files' git history is naturally append-only).

## Record schema (`data/live/journal/activity/<date>.jsonl`)

Every record carries `ts` (UTC ISO timestamp) and `run_id` (`GITHUB_RUN_ID` when running as a workflow, else a generated local id) — `run_id` is the join key `render_operational_journal()` uses to group scattered records back into "here's everything that happened in this one execution."

| `type` | Fields | Written when |
|---|---|---|
| `scan` | `symbol`, `candles_processed` | Each symbol successfully polled |
| `decision` | `opportunity_id`, `symbol`, `strategy_id`, `verdict`, `ios`, `itqs`, `reasons_against`, `session` | Every `LiveDecisionEngine` verdict |
| `trade_opened` / `trade_closed` | `position_id`, `symbol`, `strategy_id`, (`direction` / `realized_pnl`, `reason`) | Only when a paper broker is actually running (`LiveOrchestrator`) |
| `feed_error` | `symbol`, `detail` | A `ProviderConnectionError` for that symbol this cycle |
| `data_quality_warning` | `symbol`, `detail` | Reserved for future data-quality checks (gap detection etc.) |
| `notification_sent` | `alert_type` | Not currently used directly -- `cycle_summary.notifications_sent` is the aggregate the report reads |
| `cycle_summary` | `workflow_status`, `runtime_seconds`, `symbols_scanned`, `candles_processed`, `signals_detected`, `approved`, `rejected`, `notifications_sent`, `memory_mb`, `workflow_name` | Once, at the end of every script execution (in a `finally` block, so it's written even when the run fails partway through) |

## Known, honestly-stated gaps

- **`reconnect_count` is always `None`.** The stateless scan script calls `provider.connect()` fresh every 5 minutes rather than maintaining a persistent connection through `FeedManager` (which DOES implement reconnect-with-backoff, but isn't used by `telegram_scan_and_notify.py` -- see that script's own docstring). "Reconnecting" isn't a real event in this deployment; reporting a fabricated count would violate the task's own "never invent conclusions" rule.
- **Market regime drift is not computed.** It would require `src.research.market_conditions`' classification wired into the live pipeline, which doesn't exist yet. `detect_drift()` deliberately omits this rather than guess.
- **`expected_scans`/`missed_scans` are derived from the observed time span of today's OWN records**, not from a hardcoded "288 scans/day" assumption -- a partial day (platform just started, or the report is generated mid-day) is never misreported as having missed scans it was never scheduled to run yet.
