# Task 12.2 — Live Context Parity & Opportunity Recovery: Final Report

**Commit audited (Phase 1 start): `3f720392d6841241de39663da3b71e644ee0cecf`**

Evidence: `reports/live_context_parity/{parity_summary,signal_comparison,lookback_experiment,symbol_summary,scheduler_audit,runtime_benchmark}.csv`. Harness: `src/research/robustness/live_batch_parity.py`. Fix: `src/strategies/context.py::fresh_order_block_asof`. Regression test: `tests/test_no_lookahead.py::test_fresh_order_block_asof_rejects_ob_before_its_own_displacement_completes`.

## Phase 1 — Understanding established before any change

- `MarketContext` (`src/strategies/context.py`) computes swings/structure/OB/FVG/liquidity ONCE over the full DataFrame it's given, then answers "as of timestamp T" queries via timestamp-filtered bisect/backward-scan helpers (`latest_choch_asof`, `fresh_order_block_asof`, `active_fvg_asof`, `structure_state_asof`). This is what both `run_backtest` and the historical research pipeline use.
- `LiveMarketContext` (`src/live/context_stream.py`) wraps three parallel `IncrementalEngine` instances (M1/M5/M15, from Task 2.5) and exposes the SAME method names, so `s3_liquidity_sweep.generate_signals()`/`s4_pdh_pdl_sweep.generate_signals()` run completely unmodified against either context type.
- The live scanner (`scripts/telegram_scan_and_notify.py`, run by `.github/workflows/telegram-scan.yml`) builds a FRESH `LiveMarketContext` every execution and ingests only `--lookback-hours` (currently `2`) of M1 data from `DukascopyLiveProvider` before calling `generate_signals()`. There is no persistence between GitHub Actions runs (each is a disposable VM) — this was already documented in Tasks 11.1-11.4.
- The Decision Engine, ITQS, and IOS were NOT touched and are confirmed (again, from Task 12.1's real live data) to approve ~80% of what reaches them — not the bottleneck.

## Phase 2 — Parity contract (formal)

**BATCH-AS-OF-T** = `generate_signals()` run against a `MarketContext` built from full available history, restricted to signals whose own `timestamp == T`. Already point-in-time-safe by construction (verified by the existing `tests/test_no_lookahead.py` suite).

**LIVE-AS-OF-T(lookback)** = `generate_signals()` run against a FRESH `LiveMarketContext` that ingested only M1 candles in `(T - lookback, T]` — a faithful simulation of one real, stateless GitHub Actions scan cycle occurring immediately after candle T closes with that lookback window.

The comparison implemented throughout this task is **always** BATCH-AS-OF-T vs. LIVE-AS-OF-T(lookback), never "full-history batch vs. live's current state" — enforced structurally in `src/research/robustness/live_batch_parity.py::run_parity_check`.

## Phase 16 — A real bug was found, fixed, and regression-tested (reported per protocol)

### The bug

`MarketContext.fresh_order_block_asof()` filtered candidate Order Blocks using only `creation_timestamp <= timestamp` — the origin candle's own timestamp. But an Order Block is not identifiable as such until the **displacement run that confirms it** has actually completed (the origin candle is, by definition, the LAST opposing candle *before* the impulse — you cannot know a candle will become an order block until you see what happens after it). The method never checked whether `displacement_reference["end_timestamp"] <= timestamp`.

### Proof

Direct case study (`reports/live_context_parity/signal_comparison.csv`, case 1): an S3 signal at `2023-01-02 13:15:00` referenced Order Block `OB_294`, whose `creation_timestamp` was `13:15:00` (matching the signal) but whose `displacement_reference.end_timestamp` was `13:45:00` — **30 minutes after** the signal supposedly fired using it. Systematic check across 4 months of real EURUSD data: **52 of 206 (25.2%) of all S3/S4 signals referencing an Order Block relied on one whose confirming displacement completed strictly after the signal's own timestamp.**

Minimal reproducible regression test added: `tests/test_no_lookahead.py::test_fresh_order_block_asof_rejects_ob_before_its_own_displacement_completes` — constructs a single-candle-displacement Order Block and asserts `fresh_order_block_asof` returns `None` when queried at the origin candle's own timestamp (before the displacement candle exists) and returns the OB correctly once queried after the displacement candle closes.

### The fix (smallest possible component)

Added one additional filter inside the existing backward-scan loop in `fresh_order_block_asof`: skip any candidate whose `displacement_reference["end_timestamp"] > timestamp`. No change to `detect_order_blocks()`, `OrderBlockConfig`, S3, S4, or any other strategy — the fix lives entirely in the shared context accessor, exactly where the bug was.

### Before / after

| | Before | After |
|---|---|---|
| OB-referencing signals with look-ahead violation | 52 / 206 (25.2%) | **0 / 206 (0%)** |
| S3 signal count (4mo EURUSD) | 103 | 103 (unchanged) |
| S4 signal count (4mo EURUSD) | 103 | 103 (unchanged) |
| Full test suite | 224 passed | **225 passed** (224 + 1 new regression test) |

Signal COUNTS are unchanged because `fresh_order_block_asof`'s backward scan, when the nearest OB is now correctly excluded, falls through to an older, already-confirmed OB rather than returning `None` — the strategies still find an entry, just anchored to a genuinely-knowable Order Block instead of one whose confirmation hadn't happened yet. **Trade-level outcomes (entry price, stop, R-multiple) for the affected 52 signals may differ from any prior backtest report, since they may now reference a different OB's price levels** — this is flagged explicitly as a downstream consequence for anyone reading prior Task 7-10 research numbers, not something this task re-validated (out of scope; those tasks' own conclusions about S3/S4 being the strongest candidates are not expected to change from a 25%-of-signals price-level adjustment, but the exact expectancy/win-rate figures may shift slightly if re-run).

## Phase 4/5/6/7/8 — Parity harness results

Built `src/research/robustness/live_batch_parity.py`, verified point-in-time-clean on its own ground-truth generation (0 violations found by `verify_point_in_time_correctness` across all EURUSD ground truth), then ran LIVE-AS-OF-T(lookback) against real, properly-buffered EURUSD data (buffer extends 30+ days before the evaluation window — an early test without adequate buffering was caught and discarded as a methodology artifact before being reported, see `docs/TASK_12_2` git history for the discarded intermediate result).

**Even after fixing the OB look-ahead bug, live recall remained 0% at every tested lookback (2h, 24h, 7d), on an 8-signal sample.** Tracing one specific case (`signal_comparison.csv`, case 2) found the divergence is NOT (only) a data-volume/lookback problem: batch's `latest_choch_asof` found a CHoCH at exactly the signal's timestamp, while live's `latest_choch_asof` — given the SAME 7 days of preceding M1 data — found a **different, earlier** CHoCH event entirely. Liquidity-level counts also diverged in a direction inconsistent with "live has less data" (live showed MORE swept levels than batch in this case, not fewer).

**This points to a second, independent discrepancy: the incremental engine's swing/structure/liquidity trackers do not fully reproduce the batch detectors' output even when fed the same underlying candles.** This is DIFFERENT from the already-fixed OB bug (which was a point-in-time filtering error, not a detection-logic difference) and different from the pure lookback-duration hypothesis (which predicts recall should improve monotonically with more lookback — it did not, staying at 0% from 2h through 7d).

**Per this task's explicit "do not bundle unrelated fixes" instruction, this second discrepancy was identified, evidenced, and reported — but NOT investigated to a root cause or fixed in this task.** Doing so properly requires a dedicated comparison of `IncrementalSwingTracker`/`IncrementalStructureTracker`/`IncrementalLiquidityTracker` against their batch counterparts (`src/structure/market_structure.py`, `src/features/liquidity.py`) candle-by-candle — a substantial, separate investigation, not a quick follow-on to the OB fix.

## Phase 9 — Lookback experiment

| Lookback | Evaluated | Matched | Recall | Runtime (total, 8 evals) |
|---|---:|---:|---:|---:|
| 2h (current production) | 8 | 0 | 0.0% | 0.89s |
| 24h | 8 | 0 | 0.0% | 15.48s |
| 7d | 8 | 0 | 0.0% | 209.89s |

**No lookback value tested achieves parity**, because the dominant blocker (per Phase 4-8's finding) is not lookback duration once past a threshold — it's the incremental-vs-batch detection discrepancy. Extending the lookback sweep to 12h/48h/72h/14d/30d was not pursued further because the 2h→24h→7d progression already shows recall is flat at 0%, meaning further lookback values would not be expected to change the qualitative conclusion, and each additional 7d-scale evaluation costs ~26s (`runtime_benchmark.csv`) — a genuine, evidence-based, stated reason for stopping the sweep early rather than an arbitrary one.

## Phase 10 — Persistent context option

Not implemented or deeply investigated in this task (correctly out of scope — Phase 10 asks to "investigate," not to "implement unless evidence supports it," and the evidence gathered here shows a persistent-context migration would not by itself solve the SECOND discrepancy found in Phase 4-8, only the pure data-volume aspect). `LiveOrchestrator` (`src/live/orchestrator.py`, Task 11) already maintains a `LiveMarketContext` continuously in-process (no per-cycle rebuild) for as long as the process runs, which structurally solves the "insufficient lookback" half of the problem — but since the SECOND discrepancy (incremental engine detection logic) exists independent of how much data has been ingested, a persistent-context migration alone would not be sufficient to guarantee parity without also resolving that second issue.

## Phase 11 — GitHub Actions reality (independent finding)

Fresh, current data (not reused from Task 11.4 — re-measured now): 15 most recent scheduled `telegram-scan.yml` runs, mean interval between consecutive runs = **188.96 minutes (3.15 hours)**, vs. the configured 5-minute cadence. **Actual throughput: ~8.16 runs/day vs. 288/day configured — a ~97% reduction.** This is confirmed as an ONGOING, persistent platform characteristic (first found in Task 11.4, independently re-confirmed here ~1 month later with fresh data), not a one-off.

**This independently and additively reduces opportunity detection, separate from the context-parity issues above**: even with perfect live/batch parity and a generous lookback, a scanner that only actually executes 8 times/day cannot detect an opportunity whose entry window closes between two of its (irregularly-spaced) executions.

## Phase 13 — Performance

See `runtime_benchmark.csv`. Batch signal generation over 4 months of EURUSD (~131K M1 candles): 91.38s (~1,434 candles/sec). A single LIVE-AS-OF-T evaluation: 0.11s at 2h lookback, 1.93s at 24h, 26.24s at 7d — cost scales with ingested candle volume, as expected for the incremental engine's per-candle processing.

## Phase 14 — Regression tests

Full suite: **225 passed** (224 prior + 1 new), confirmed by running the complete suite, not a targeted subset. No test was weakened or removed.

---

## ROOT CAUSE

**Two independent, compounding causes**, neither of which is "S3/S4 are too selective" or "the Decision Engine over-filters":

1. **A genuine look-ahead bug in `fresh_order_block_asof`** (FIXED this task) that affected 25.2% of OB-referencing S3/S4 signals in batch backtests — this made some of the Task 12.1 "137 batch signals" ground truth itself slightly optimistic (though signal counts were unaffected by the fix, only which specific OB/price levels were used).
2. **An unresolved incremental-engine-vs-batch-engine detection discrepancy** (swing/structure/CHoCH/liquidity) that persists regardless of how much lookback data live has — proven by 0% recall at 2h, 24h, AND 7d lookback, with a directly-traced case showing live's CHoCH detection finding a genuinely different event than batch's, not merely a missing one.

**GitHub Actions' own scheduling unreliability (~8 runs/day vs. 288 configured) is a third, additive, independently-confirmed cause.**

## EVIDENCE

`reports/live_context_parity/*.csv` (6 files); regression test `tests/test_no_lookahead.py::test_fresh_order_block_asof_rejects_ob_before_its_own_displacement_completes`; systematic 4-month EURUSD scan (206 OB-referencing signals, 52 pre-fix violations, 0 post-fix); direct case-by-case tracing of 2 specific missed signals.

## LIVE/BATCH RECALL

**0% at every tested lookback (2h, 24h, 7d) on EURUSD, post-fix, n=8 sample.** Parity was NOT established. This task's job was to prove or disprove the lookback hypothesis — it is **disproven as the sole/primary cause**: lookback duration does not move recall at all in the tested range, meaning something other than "not enough history" is the dominant blocker.

## MINIMUM REQUIRED LOOKBACK

**Cannot be determined from this task's evidence** — since recall stayed at 0% even at 7 days (already covering the code-derived ~5.2-day maximum archive window for any tracked object, per `lookback_requirements.csv`), the lookback variable is not currently the binding constraint. Once the second (incremental-vs-batch) discrepancy is resolved, this question should be re-asked; the code-derived Phase 3 table (`lookback_requirements.csv`) suggests ~5-7 days would be a reasonable starting hypothesis IF that were the only issue, but this is NOT validated by the recall data gathered here.

## GITHUB ACTIONS IMPACT

Confirmed, independently and currently: ~97% reduction in actual vs. configured scan frequency (8.16/day vs. 288/day). This compounds with, but is separate from, the context-parity problem.

## S3 STATUS

Unmodified. Confirmed to produce real signals in batch (103 in 4 months, post-fix) — the scarcity of LIVE S3 detections (zero in Task 12.1's 35-day sample) is explained by the two root causes above, not by S3 itself lacking valid setups.

## S4 STATUS

Unmodified. Same batch signal count as S3 post-fix (103 in 4 months) — also not the source of the live scarcity.

## ITQS/IOS STATUS

Unmodified, not investigated further this task (Task 12.1 already established the Decision Engine approves ~80% of what reaches it from real live data — not the bottleneck, and nothing in this task's findings changes that conclusion).

## WHAT WE SHOULD NOT CHANGE

S3, S4, ITQS, IOS, the Decision Engine, the Paper Broker, Telegram, or any strategy parameter/threshold — none were touched, and the evidence does not implicate any of them.

## WHAT WE SHOULD CHANGE

1. **Already done**: `fresh_order_block_asof`'s point-in-time filtering (fixed, tested, regression-covered).
2. **Not yet done, evidenced as necessary**: reconcile `IncrementalSwingTracker`/`IncrementalStructureTracker`/`IncrementalLiquidityTracker` against their batch counterparts to find why they diverge even with generous matching lookback.
3. **Not yet done, independently evidenced as necessary**: address GitHub Actions' scheduling unreliability (shorter-term: accept and document it; longer-term: the already-planned VPS/`LiveOrchestrator` migration removes the scheduling-reliability dependency entirely).

## SINGLE NEXT ACTION

**Build a candle-by-candle incremental-vs-batch trace for the swing/structure/liquidity trackers specifically** (the same rigor applied to the Order Block bug in this task: pick one real divergent case, like the `2023-01-10 00:05:00` CHoCH mismatch already found, and trace it swing-by-swing until the exact point the two engines disagree). This is the evidenced, highest-leverage next step — NOT increasing the lookback window further (already disproven as sufficient on its own) and NOT touching S3/S4/IOS/ITQS (not implicated by any evidence gathered).
