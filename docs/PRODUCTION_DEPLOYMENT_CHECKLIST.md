# Production Deployment Checklist

Gate before this platform touches live capital. Every item is either DONE (with evidence cited) or NOT DONE (with what's required to close it) — no item is marked done on assumption.

## Research & Strategy Validation

- [x] Strategies backtested on real historical data (Task 7.3/7.4/8) — EURUSD full history, 6 other symbols 6-month depth.
- [x] Strategy ranking with multi-metric evidence (Task 8) — S3/S4 selected, S1 rejected, S2/S5 held as secondary.
- [x] Feature importance / edge source identified (Task 9) — Order Block freshness/quality, statistically validated (p<0.001/p<0.01).
- [x] Trade-quality scoring validated against outcomes (Task 9 ITQS, Task 10 IOS) — both show real, non-zero, positive correlation with actual results.
- [ ] **Multi-year data for the 6 non-EURUSD symbols** — currently only 6 months each (Task 8 §3 scope note). Required before those symbols receive full-size allocation.
- [ ] **XAUUSD research** — excluded from scope in Tasks 8/9/10; requires its own data acquisition + Task 8/9-style analysis before inclusion.

## Decision Engine

- [x] Unified opportunity queue across strategies (Task 10 Phase 1).
- [x] Explainable ranking (IOS, Task 10 Phase 2) — no black-box components, every weight traceable to a specific Task 8/9 finding.
- [x] Portfolio allocation limits (Task 10 Phase 3) — capacity, currency, strategy, correlation.
- [x] Institutional risk layer (Task 10 Phase 5) — daily/weekly/monthly loss, portfolio heat, session exposure.
- [x] Every decision explainable (Task 10 Phase 7) — ✓/✗ reason lists on every Decision object.
- [x] Selectivity validated against a naive baseline (Task 10 Phase 8 paper trading) — +26.3% expectancy, +18.2% profit factor on the selected subset.
- [x] **Live/streaming signal ingestion** — Task 11 builds a live market-data engine (Dukascopy near-live polling, verified against the real network), an incremental market-context adapter over the existing Task 2.5 engine, and a live strategy runner that produces real Opportunities from live candles. See `docs/PRODUCTION_READINESS_REPORT_TASK11.md` for the "near-live polling, not sub-second streaming" caveat.
- [x] **Broker/execution integration** — a full simulated paper broker (Task 11 Phase 6) now exists: market/pending orders, SL/TP, partial exits, breakeven, trailing stop, slippage, spread, commission, margin, swap. **Still simulated only — no real broker/venue connection exists** (explicitly out of scope; see Task 11's MT5 provider, which refuses to fake a connection).

## Risk Management

- [x] Account-level loss limits defined and shown to bind in practice (Task 10 §"Why opportunities were rejected" — loss limits are the majority rejection reason, confirming they are not vacuous).
- [ ] **Risk-base compounding/reset policy** — the current risk layer uses a fixed `starting_balance` reference for daily/weekly/monthly % limits; a production account needs an explicit decision on whether risk limits scale with growing equity or reset periodically (flagged, not resolved, in `READINESS_ASSESSMENT_TASK10.md`).
- [ ] **Real-money position sizing validation** — `RiskConfig`/`AllocationLimits` risk percentages have not been stress-tested against real account constraints (margin, leverage limits, broker minimum lot sizes).

## Known Platform Bugs

- [x] `src.backtest.risk.RiskTracker.consecutive_losses` fixed (Task 11 Phase 1) — genuine time-bounded cooldown (`locked_out_until`), not a permanent lockout; expires on elapsed time regardless of trade activity during the pause, and a winning close clears it immediately. Verified via two new regression tests plus the full 177-test suite.
- [x] `FVGAlignment` confidence factor fixed (Task 11 Phase 1) — the FVG lookup now always runs (S2/S3/S4/S5), so the confidence score reflects the real presence/absence of an FVG instead of a hardcoded neutral 0.5. Entry-gating logic (`require_fvg`) unchanged.

## Live Infrastructure (Task 11, new)

- [x] Live market data engine — provider architecture mirroring the historical pipeline (`PROVIDERS` registry), Dukascopy near-live polling provider verified against the real network, HistData directory-watcher, MT5 adapter (interface-complete, correctly refuses to fake a connection without real credentials).
- [x] Feed manager — automatic reconnect (exponential backoff), heartbeat, gap detection + recovery, UTC normalization, per-symbol independent polling.
- [x] Incremental market context — wraps the existing Task 2.5 `IncrementalEngine` in a `MarketContext`-shaped adapter so S3/S4 run UNCHANGED against live data; verified end-to-end against real Dukascopy data.
- [x] Live strategy engine, continuous decision engine (portfolio heat/currency/strategy exposure carried across cycles), paper broker, event logger (single ordered audit trail across every module), monitoring dashboard, notification system (console/file live; Telegram/Discord/Email code-complete but require real credentials not present in this environment).
- [x] Forward paper trading framework built and exercised in a short bounded demo against real live data (see `docs/PRODUCTION_READINESS_REPORT_TASK11.md`) — **NOT** the weeks-long unattended run needed to actually validate live behavior; see that report for the explicit evidence gap.
- [ ] **No real broker connection** — the paper broker is a simulator; MT5 (or any other execution venue) integration requires real credentials and a running terminal, neither available here.
- [ ] **Weeks-long forward-test evidence** — not collected. This is the single largest remaining gate before any live-capital conversation is meaningful; see the Production Readiness Report.

## Governance / Process

- [x] Every strategy, filter, and score change in Tasks 8-10 is backed by a cited measurement, not intuition (explicit engineering principle across all three tasks, followed throughout).
- [ ] **Paper-trading period**: a live forward-testing framework now exists and has been exercised (Task 11), but only for a short bounded demo, not the recommended 3-6 month duration (`READINESS_ASSESSMENT_TASK10.md`). This remains open.
- [ ] **Independent review sign-off**: no external/independent review of the decision engine's logic has occurred (this document itself is not a substitute for one).

## Summary

**Still not ready for live capital — but the infrastructure gate is now closed.** Task 11 fixed both known platform bugs and built every piece of live infrastructure the platform was missing (live data, streaming context, live strategy/decision engines, paper broker, event logging, monitoring, alerting, forward-testing framework). What remains is empirical, not architectural: real broker/venue integration (deliberately not attempted — no credentials, no fabrication) and, above all, the weeks-long forward-paper-trading evidence needed to confirm IOS/ITQS/S3/S4 hold up on live markets the way they did in backtest. See `docs/PRODUCTION_READINESS_REPORT_TASK11.md`.
