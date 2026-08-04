# Live Deployment Guide — Task 11 Infrastructure

How to run the platform's live infrastructure, what it needs, and what it does NOT yet do. This guide is about turning the code on, not about whether it should be trusted with real money (see `docs/PRODUCTION_READINESS_REPORT_TASK11.md` for that).

## What "live" means here today

**Paper trading against a near-live data feed. No real broker, no real money, no automatic execution against any venue.** Every safeguard in this codebase (MT5's refusal to fake a connection, the notification channels' `NotConfiguredError`, the paper broker's pure simulation) exists specifically to prevent this guide from being mistaken for a real-capital deployment guide.

## Prerequisites

- Python environment with the project's existing dependencies (`pandas`, `numpy`, `psutil` — already used by prior tasks) plus outbound HTTPS access to `datafeed.dukascopy.com` (the same endpoint Task 8's historical downloader already uses).
- No API keys or accounts needed for the default configuration (Dukascopy's public archive, console/file notifications).

## Component map

```
DukascopyLiveProvider ──▶ FeedManager ──▶ LiveMarketContext (per symbol)
                                              │
                                              ▼
                                     LiveStrategyRunner (S3, S4)
                                              │
                                              ▼  Opportunities
                                    LiveDecisionEngine (IOS, allocation, risk)
                                              │
                                              ▼  EXECUTE decisions
                                        PaperBroker (fills, SL/TP, management)
                                              │
                    ┌─────────────────────────┼─────────────────────────┐
                    ▼                         ▼                         ▼
              EventLogger              Dashboard (HTML)         NotificationRouter
           (events.jsonl)                                    (console/file/Telegram*/
                                                                Discord*/Email*)
                                                        * code-complete, needs real credentials
```

Everything above is wired by `src.live.orchestrator.LiveOrchestrator`; `scripts/run_forward_paper_trading.py` is the only entry point that actually runs it.

## Configuration

All via `LiveOrchestrator.__init__` kwargs (see `src/live/orchestrator.py`):

| Parameter | Default | Notes |
|---|---|---|
| `symbols` | required | One `LiveMarketContext`/`LiveStrategyRunner` per symbol; account/broker shared. |
| `execution_config` | `ExecutionConfig()` | Spread, slippage, commission, leverage, swap — same class the backtest engine uses (Task 4), now extended with `leverage`/`swap_*_per_lot_per_day` (Task 11). |
| `management_config` | `ManagementConfig()` | Breakeven/trailing-stop rules — identical semantics to backtest (functions are literally reused, not reimplemented). |
| `risk_limits` | `RiskLimits()` | Task 10's account-level risk layer, unchanged. |
| `starting_balance` | `10_000.0` | Paper account only. |
| `lots_per_trade` | `0.1` | Fixed lot size per EXECUTEd opportunity. **Not yet risk-based sizing** — Decision.allocated_risk_pct (IOS-tier-based, from Task 10) is computed but not currently converted into a lot size; this is a known simplification, not a silent gap (flagged in the readiness report). |

## Enabling real notification channels

Set the matching environment variables before starting the orchestrator; each channel is otherwise skipped (never fabricated as "sent"):

```bash
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=...
export DISCORD_WEBHOOK_URL=...
export SMTP_HOST=... SMTP_PORT=587 SMTP_USER=... SMTP_PASSWORD=... ALERT_EMAIL_TO=...
```

`NotificationRouter` (in `src/live/orchestrator.py`) already constructs all five channels; supplying credentials is the only change needed to make Telegram/Discord/Email start actually sending.

## Running

```bash
python scripts/run_forward_paper_trading.py \
  --cycles 20 --interval 60 \
  --symbols EURUSD,GBPUSD \
  --lookback-hours 6 \
  --data-dir data/live/forward_test
```

See `docs/OPERATIONAL_RUNBOOK_TASK11.md` for interpreting output, alerts, and incidents once it's running.

## What this guide deliberately does NOT cover

- **Connecting a real broker.** `src/live/providers/mt5_live.py` is interface-complete but its `connect()` intentionally raises with a clear message about the missing `MetaTrader5` package, a running MT5 terminal, and real account credentials — none of which exist in this environment. Wiring a real broker means: (1) installing/running an actual MT5 terminal or another venue's SDK, (2) real account credentials, (3) replacing `PaperBroker` calls in `LiveOrchestrator.run_cycle()` with real order-placement calls, and (4) a live-execution risk review this document is not a substitute for.
- **Running unattended for weeks.** Nothing here provides process supervision, log rotation, or crash recovery beyond a single Python process. A production deployment would need a process manager (systemd/supervisor/container orchestrator) and the persistence layer flagged as missing in the runbook (§6).
- **Position-size risk conversion.** `lots_per_trade` is fixed; converting `Decision.allocated_risk_pct` into an actual lot size (using stop-distance and account balance, the same way `src.backtest.risk` sizing methods already do for the backtest engine) is a small, well-scoped follow-up, not done here.
