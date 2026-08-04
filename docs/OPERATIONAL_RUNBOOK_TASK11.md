# Operational Runbook — Live Market Infrastructure & Forward Paper Trading

Covers day-to-day operation of the Task 11 live platform: starting a run, reading its output, and responding to the failure modes it's designed to surface. This is a paper-trading runbook — nothing here ever touches real capital or sends a real order.

## 1. Starting a run

```bash
python scripts/run_forward_paper_trading.py --cycles 20 --interval 60 --symbols EURUSD,GBPUSD --lookback-hours 6 --data-dir data/live/forward_test
```

- `--cycles`: number of poll/decide/trade cycles to run before exiting. There is no "run forever" mode yet — see §6.
- `--interval`: seconds between cycles. Dukascopy's near-live provider only ever returns newly-*completed* hours, so intervals shorter than the time until the next hour boundary will mostly return zero new candles — this is expected, not a bug (see §4).
- `--symbols`: comma-separated. Each gets its own `LiveMarketContext` + `LiveStrategyRunner`; the account/decision engine/broker are shared across all of them (portfolio-level risk is meaningful across symbols).
- `--lookback-hours`: how far back the FIRST poll reaches. Only affects cycle 1 — subsequent cycles poll forward from the last-seen candle per symbol.

## 2. What gets written

All under `--data-dir` (default `data/live/forward_test`):

| File | Contents |
|---|---|
| `dashboard.html` | Refreshed every cycle. Open in any browser, reload to see current state. |
| `events.jsonl` | Append-only, one JSON object per line, every event from every module (feed manager, incremental engine, decision engine, broker) in one global sequence. Never rotated or truncated by the platform — clear it yourself between unrelated runs if you want a clean file. |
| `notifications.log` | Every alert raised (console output mirrors this). |

Read the dashboard for current state; read `events.jsonl` for "what exactly happened and in what order" (e.g. reconstructing the brief's example chain: sweep -> CHoCH -> IOS -> approval -> open -> TP1 -> breakeven -> close).

## 3. Reading the dashboard

- **System Health** — CPU/memory of the Python process running the orchestrator, and wall-clock uptime since the orchestrator was constructed.
- **Connected Providers** — one row per configured provider (currently one, Dukascopy). `connected: False` or a nonzero `consecutive_failures` means the feed is unhealthy — see §5.
- **Market Status** — `LIVE` if the latest candle for that symbol is under 5 minutes old, `STALE` otherwise, `NO_DATA` if nothing has arrived yet.
- **Open / Pending Trades**, **Portfolio & PnL**, **Risk** — self-explanatory; `Portfolio Heat` should never exceed `AllocationLimits.max_portfolio_risk_pct` (3.0% by default) — if it does, that's a bug, not a policy decision (the decision engine's own allocation check should have prevented it).

## 4. Normal, expected behavior (not incidents)

- **Zero new candles most cycles.** Dukascopy publishes hourly archives; polling every 60s will see 0 new M1 candles for ~59 of every 60 cycles, then ~60 new candles at once when an hour completes. This is the platform's honestly-documented "near-live polling, not streaming" limitation (see `docs/PRODUCTION_READINESS_REPORT_TASK11.md`).
- **Zero opportunities for long stretches.** S3/S4 are deliberately selective (Task 8/9 found they fire a handful of times per symbol per week). Seeing no opportunities for hours is normal, not a malfunction.
- **Weekend gaps.** `FeedManager._detect_gaps` explicitly does not flag the Friday-close -> Sunday/Monday-reopen gap as a data problem.

## 5. Incident response

| Symptom | Likely cause | Action |
|---|---|---|
| `Data Feed Disconnected` alert | Dukascopy endpoint unreachable, or every hour in the poll window errored | Check network connectivity. `FeedManager.ensure_connected()` already retries with exponential backoff (`ReconnectPolicy`, default 5 attempts) before this fires — if it still fires, the outage is real, not transient. No action needed beyond waiting/monitoring unless it persists past several cycles. |
| `gap_detected` event with no matching `gap_recovered` | A window of candles is genuinely missing (not weekend) and the recovery re-poll also failed | Check `data_quality` in the analytics report (`src/live/analytics.py`) for the affected symbol's `gap_recovery_rate`. A persistently low rate across a run means the provider itself is dropping data, not a one-off blip. |
| `Risk Limit Reached` alert | Portfolio heat at/above `max_portfolio_risk_pct` | Expected behavior — the decision engine will POSTPONE/IGNORE new opportunities until an open position closes and frees capacity. No manual intervention needed; this is the risk layer doing its job. |
| `order_rejected_insufficient_margin` event | `PaperBroker.free_margin()` insufficient for the requested lot size | With `lots_per_trade=0.1` and a $10,000 starting balance this should not occur under default config; if it does, it means an EXECUTEd decision size doesn't match available margin — check `ExecutionConfig.leverage` and `lots_per_trade` are set consistently with the account size passed to `LiveOrchestrator`. |
| Dashboard `Market Status` stuck on `STALE` | Feed genuinely stalled, or the orchestrator loop itself stopped running | Check the process is still alive and `events.jsonl` is still growing. |

## 6. Stopping / restarting

- Stop: interrupt the process (Ctrl-C) or let `--cycles` run out. `orchestrator.shutdown()` closes the event log file handle cleanly.
- **No persistence/resume across restarts yet.** `LiveMarketContext`, `LiveDecisionEngine`, and `PaperBroker` are all in-memory only in Task 11's implementation — restarting the orchestrator starts a fresh context (no open positions, no portfolio history). `IncrementalEngine` itself DOES support `save()`/`load()` (inherited from Task 2.5) but `LiveMarketContext`/`LiveOrchestrator` do not yet wire that up. **This is a real gap for anything beyond a single bounded session** — flagged in the Production Readiness Report, not silently glossed over.
- Before any real continuous deployment: add persistence for `LiveMarketContext` (already has the underlying `IncrementalEngine.save()`/`.load()` to build on), `PaperBroker` open positions, and `LiveDecisionEngine` state, so a restart resumes rather than resets.
