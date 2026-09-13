# Task 12.3 — Incremental Structure Parity: Swings → BOS/CHoCH → Liquidity

Starting commit: `973207c97335779fff5caaf6f7828bf3793f6136` (Task 12.2, pushed to `origin/main`).

## 1. Executive Summary

Task 12.2 traced a concrete divergence at EURUSD, 2023-01-10 00:05:00 UTC, where the
live (incremental) and batch engines disagreed on the most recent M5 CHoCH. This task
found the root cause: `IncrementalEngine.process_candle()` (`src/engine/engine.py`)
was ingesting a newly confirmed swing into the structure and liquidity trackers **one
candle too early** relative to the batch reference implementation's semantics. The fix
is a three-line change (buffer newly confirmed swings for one candle before ingesting
them). After the fix, a full-history incremental run reproduces the batch engine's
swing and structure/BOS/CHoCH event streams **exactly** (596/596 M5 events, 201/201 M15
events, 1639/1639 swings, zero symmetric difference) over a real 3-week EURUSD window
spanning the known divergence.

This is a genuine, provable, minimal bug fix — not a threshold change, not a strategy
change, not a lookback increase. It fully resolves the swing/BOS/CHoCH divergence Task
12.2 found.

**However**, this fix does **not** resolve the live scanner's 0% S3/S4 recall problem.
Re-running the Task 12.2 parity harness after the fix, at 2h and 24h lookback windows,
still shows 0% recall. That is because the live scanner's actual deployment mode
(GitHub Actions rebuilding a fresh `LiveMarketContext` from a short lookback window
every cycle) cannot reproduce the `active_high`/`active_low` state batch accumulates
from months of history — that is a **separate, architectural, since-inception
warm-up problem**, not fixed by this task and explicitly out of scope for it (Task
12.3 was scoped to "same candles + same config + same point-in-time information =
same structural state," which is now true; it was never scoped to "insufficient
lookback," which Task 12.2 already investigated and could not resolve by widening the
window alone).

A second, unrelated divergence was found and is **not fixed**: liquidity levels
diverge substantially between batch (76 levels) and live (858 levels) on the same
window, because batch clusters equal-highs/lows globally across the whole dataset
while incremental only merges a new swing into a still-ACTIVE (never-yet-swept)
level. This is a distinct algorithmic difference, documented in Section 12, and
recommended as Task 12.4 work.

A third finding, purely about performance, is documented in Section 9: `registry.
refresh()` rescans unbounded lists every candle, making a multi-month incremental
backfill dramatically slower than it should be. Also not fixed here (out of scope,
would need its own task).

## 2. Phase 1 — Repository Audit

Read in full: `src/structure/swings.py` (batch swing detection),
`src/structure/market_structure.py` (batch BOS/CHoCH), `src/features/liquidity.py`
(batch liquidity), and the incremental equivalents in `src/engine/incremental.py`
(`IncrementalSwingTracker`, `IncrementalStructureTracker`, `IncrementalLiquidityTracker`)
plus the orchestrating `src/engine/engine.py::IncrementalEngine.process_candle()`.

Key finding from static reading alone: the swing detection algorithms are structurally
identical (same strict fractal inequalities, same window sizes, same
`confirmed_timestamp = candle.timestamp + interval` formula on both sides) and the
BOS/CHoCH break-testing logic is also identical (same `require_close_beyond_level`
branch, same "keep most-recently-confirmed unbroken swing" rule, same bullish-then-
bearish per-candle check order). The divergence was therefore not in the detection
*rules* themselves but in **when** a freshly confirmed swing becomes visible to the
structure/liquidity trackers relative to batch.

## 3. Phase 2–3 — Candle-by-Candle Trace and First Divergence

Built a diagnostic harness (`phase2_full_history_check.py`, scratchpad) that runs:
- **Batch ground truth**: `MarketContext(symbol, m1=window).structure_events("M5"/"M15")`
  over one consistent EURUSD M1 slice.
- **Full-history incremental**: a fresh `LiveMarketContext` fed *every* M1 candle in
  the *same* slice from the *same* starting point (no windowing/truncation) — this
  isolates the algorithm-equivalence question from the lookback-sufficiency question
  Task 12.2 already answered.

One consistent dataset was used throughout for both sides, per this task's explicit
instruction not to repeat Task 12.2's dataset-mismatch mistake.

Symmetric-diff of the two M5 structure-event streams (before the fix) found 16
batch-only events and 5 live-only events, at timestamps from 2022-12-21 through
2023-01-18 — a self-resolving desync that would appear, mislabel a BOS as a CHoCH (or
vice versa) or drop an event entirely, then several events later realign. Swings
matched perfectly (1639/1639) at every field, proving the divergence was isolated to
the structure/liquidity ingestion step, not swing detection.

**Classification: G — timestamp/indexing** (an off-by-one-candle activation timing
bug), not a swing (B), BOS (C) or CHoCH (D) *rule* divergence, and not an
initialization/warm-up (F) effect — the full-history run ruled that out since both
sides started from the identical empty state.

## 4. Phase 4 — The Known EURUSD Case, Explained

`confirmed_timestamp` (`src/structure/swings.py`) is explicitly defined as the CLOSE
of the confirming candle, i.e. the OPEN of the candle *after* it — the moment the
swing becomes knowable. The batch reference (`detect_structure_events`) gates
ingestion on `confirmed_timestamp <= candle.timestamp`, walking candles in order and
treating `ts_list[i]` as "candle i has just closed." Given a swing confirmed by candle
at index `k`, `confirmed_timestamp` equals `ts_list[k+1]` — so batch's while-loop
first ingests it while processing candle `k+1`, **one candle after** the candle that
confirmed it.

`IncrementalEngine.process_candle()` previously called `structure.ingest_swing()` /
`liquidity.ingest_swing()` immediately on the `new_swings` returned by
`IncrementalSwingTracker.update(candle)` — in the **same** call that processed candle
`k` (the confirming candle itself). One candle too early.

Because a confirming candle's own high/low can never exceed the swing it confirms
(that is what makes it a fractal), this off-by-one could never let a candle break the
swing it had just confirmed. But it *did* change which level was "active" whenever an
older, still-unbroken swing was superseded by a fresher, higher-index swing on the
very candle that fresher swing was confirmed:

- **Old buggy incremental**: on candle `k` (confirming swing H2), `ingest_swing(H2)`
  fires first, immediately replacing the older active level H1 (since H2 has a higher
  `candle_index` and H1 is not yet broken). The break-test on candle `k` then checks
  against H2, not H1 — and since H2 is by definition higher/lower than whatever candle
  `k`'s own high/low/close can reach (it is the very candle that just confirmed H2 as
  the local extreme), **no break fires at all**, and H1's level is silently discarded
  without ever being tested.
- **Batch / fixed incremental**: on candle `k`, H2 is not yet ingested. The active
  level is still H1. If candle `k`'s close/high/low crosses H1, the break fires
  correctly, against H1, exactly as batch computes it.

At EURUSD 2023-01-10 00:05:00 (M5), this exact mechanism produced a chain of
mislabeled/missing events starting days earlier (first observed mismatch:
2022-12-21 16:45 BOS bearish @ 1.06049, batch-only) that eventually surfaced as the
CHoCH Task 12.2 traced.

## 5. Phase 5 — Bug, Not Architectural

This is **not** an architectural limitation. The incremental engine's per-candle,
O(1)-per-object design is fully capable of reproducing batch's semantics — it just had
the ingestion step wired to the wrong candle. Proof: a full-history incremental run,
after the one-line ingestion-timing fix, reproduces batch's structure/BOS/CHoCH event
stream byte-for-byte (Section 8). No bounded-history limitation, no confirmation-
timing limitation, no processing-order limitation was found for swings/structure. (A
distinct, *architectural* difference **was** found in liquidity clustering — see
Section 12 — which is not fixed here.)

## 6. Phase 6 — The Minimal Fix

`src/engine/engine.py`, `IncrementalEngine`:

```python
self._pending_swings: list = []   # __init__

def process_candle(self, candle):
    ...
    for swing in self._pending_swings:
        self.structure.ingest_swing(swing)
        self.liquidity.ingest_swing(swing)
    new_swings = self.swings.update(candle)
    self._pending_swings = new_swings
    ...
```

Newly confirmed swings are buffered for exactly one candle before being ingested into
`structure`/`liquidity`, reproducing batch's `confirmed_timestamp <= candle.timestamp`
gating exactly. `_pending_swings` was added to `to_dict()`/`load()` so a save/restore
cycle carries the pending buffer correctly (a swing confirmed on the very last candle
before a checkpoint must still be applied on the first candle after restore).

This preserves: point-in-time correctness (if anything, it is now *more* conservative
by one candle, never less), all existing strategy semantics (S3/S4 are unmodified;
they call `MarketContext`/`LiveMarketContext` methods, not the tracker internals), and
does not touch ITQS/IOS/Decision Engine/Telegram/GitHub Actions/S3/S4 thresholds.

## 7. Phase 7 — Regression Tests

Added to `tests/test_incremental_engine.py`:

- `test_a_fresher_swing_confirmed_on_the_break_candle_does_not_preempt_the_break` — a
  deterministic, minimal synthetic repro: an early swing high at 1.020 stays active;
  a second, higher swing high (1.060) is confirmed later, whose own confirming candle
  closes at 1.030 (above the old level, below the new one). Batch fires a bullish BOS
  breaking 1.020 on that candle; asserts the incremental engine matches exactly. This
  test fails against the pre-fix code (verified) and passes against the fix.

Pre-existing regression coverage re-verified as still passing, including
`test_incremental_swings_match_batch`, `test_incremental_structure_events_match_batch`,
`test_incremental_order_blocks_match_batch`, `test_incremental_fvg_matches_batch`, the
Task 12.2 look-ahead regression (`test_fresh_order_block_asof_rejects_ob_before_its_own_
displacement_completes`), and `test_engine_never_uses_future_candle_data`.

**Full suite: `pytest -q` → 232 passed** (231 baseline + 1 new), 0 failures, 0 skips.

The real-EURUSD full-history comparison (Section 8) was deliberately **not** added as
an automated pytest test — at ~4 minutes per run (see Section 9) it would make the
suite unacceptably slow for routine use. It is preserved as a one-off, reproducible
verification (`phase2_full_history_check.py` in the session scratchpad) and its
results are recorded here instead.

## 8. Phase 8 — Parity Recheck

EURUSD, 2022-12-20 → 2023-01-20 (real historical data,
`data/processed/historical/EURUSD_M1.parquet`), full-history incremental run (no
windowing) vs. batch, same slice:

| Primitive | Before fix | After fix |
|---|---|---|
| Swings (M5) | 1639 / 1639 match (already OK) | 1639 / 1639 match |
| Structure/BOS/CHoCH events (M5) | 596 batch vs 585 live; 16 batch-only + 5 live-only mismatches | **596 / 596, 0 mismatches** |
| Structure/BOS/CHoCH events (M15) | 201 / 201 match (already OK on this window) | 201 / 201 match |
| Liquidity levels (M5) | not compared in Task 12.2 | 76 batch vs 858 live — **still diverges (separate issue, Section 12)** |

GBPUSD/USDJPY were **not** re-run in this task — the 3-week EURUSD full-history run
alone took ~4 minutes per side due to the registry performance issue (Section 9), and
this task's time budget was allocated to finding, fixing, and proving the actual root
cause rather than repeating the same already-proven fix across symbols. Recommended as
part of Task 12.4's acceptance criteria.

**S3/S4 recall** (windowed live/batch parity harness, `src/research/robustness/
live_batch_parity.py`, same EURUSD window, batch ground truth = 94 signals):

| Lookback | Before fix (Task 12.2) | After fix |
|---|---|---|
| 2h | 0% | 0% (0/15 sampled) |
| 24h | 0% | 0% (0/15 sampled) |

**Recall did not improve.** This is expected and consistent with the root cause: the
swing-activation-timing bug only matters when comparing engines that share the same
starting history. A windowed live context is rebuilt from scratch every scan cycle and
starts with `active_high = active_low = None`, `state = "UNKNOWN"` — it has no way to
know about levels that were set by swings *before* its lookback window began. Fixing
the one-candle timing bug makes the incremental engine faithfully reproduce batch
*given the same information*; it cannot manufacture information the windowed context
was never given. This confirms Task 12.2's conclusion that "insufficient lookback
alone" and "an incremental algorithm bug" were **two separate, additive** problems —
this task fixed the second one completely; the first one (since-inception warm-up
state) remains unresolved and is architectural, requiring a persistent/checkpointed
live context (Task 12.2's originally recommended, not-yet-built "Option: persistent
context," Phase 10 below).

## 9. Phase 9 — Performance

Full-history incremental run (EURUSD, 3 weeks, 32,830 M1 candles, 3 parallel
timeframe engines M1/M5/M15): **~135 candles/sec** (242s for 32,830 M1 candles, after
the fix — the fix itself adds negligible overhead, a one-list buffer swap per candle).

This throughput is **not linear** — it visibly degrades as history accumulates:

| M1 candles processed | Elapsed |
|---|---|
| 5,000 | 8.1s (~620/s) |
| 10,000 | 24.9s (~400/s cumulative) |
| 20,000 | 90.9s (~220/s cumulative) |
| 30,000 | 201.2s (~150/s cumulative) |

**Root cause (separate finding, not fixed in this task)**: `ActiveObjectRegistry.
refresh()` (`src/engine/registry.py`) rescans `swing_tracker.confirmed_swings` (a list
that "never deletes," per its own docstring) and the order-block/FVG/liquidity
trackers' full unbounded `_objects` dicts, **every single candle**, on every one of
the three parallel timeframe engines. This is O(n) per candle where n grows with total
history processed, making a multi-month backfill effectively O(n²). A full 4.5-month
backfill attempt (145K M1 candles) was killed after 90+ minutes during this task's
investigation as clearly disproportionate — it is what first revealed this issue.

This is flagged for Task 12.4, not fixed here, per this task's explicit instruction:
"do not replace an incremental algorithm with full historical recomputation... unless
the performance impact is explicitly measured" and to keep this task's diff minimal
and focused on the actual root cause it was scoped to fix.

## 10. Phase 10 — LiveOrchestrator Implication

**Can a persistent process now reproduce batch structure?** For swings/BOS/CHoCH: yes,
*given sufficient warm-up from the true start of relevant history* — the fix proven in
Section 8 shows the incremental algorithm faithfully reproduces batch once fed the
same candles from the same starting point. It does **not** mean a short-lookback,
rebuilt-from-scratch scan cycle (the current GitHub Actions deployment mode) can
reproduce batch — that requires either:

1. **A genuinely long warm-up** — feeding enough history before "now" that
   `active_high`/`active_low`/`state` converge to what batch would compute (this was
   not bounded in this task; Task 12.2 already showed 7-day lookback is nowhere near
   enough, and there is no principled fixed answer since an active swing can, in
   principle, persist unbroken for arbitrarily long), or
2. **A persistent, checkpointed process** — one long-running `IncrementalEngine`
   instance (per symbol/timeframe) that never restarts from scratch, saving/restoring
   its full `state_dict()` (already implemented, and now includes `_pending_swings`)
   across restarts/deploys.

Given the performance finding in Section 9, option 1 (long per-cycle warm-up recompute)
would be **increasingly expensive** the longer the assumed warm-up needs to be, while
option 2 (persistent state, checkpointed once, updated incrementally per new candle)
is the architecturally correct fit for an O(1)-per-candle incremental engine — but it
requires the registry performance issue (Section 9) to be fixed first, and requires
explicit handling of restart/gap/reconnect scenarios (already partially covered by
existing `save()`/`load()` and `test_restart_recovery_matches_continuous_run`, but not
exercised against a real multi-month gap or missed-candle scenario).

Per this task's explicit instruction, **no persistence/checkpoint system was built**
here — the evidence supports recommending it, not building it inside this task.

## 11. What Was Not Changed

Confirmed unmodified: S3 (`s3_liquidity_sweep`), S4 (`s4_pdh_pdl_sweep`), ITQS, IOS,
the Decision Engine, the Paper Broker, Telegram notification code, GitHub Actions
scheduling, all strategy thresholds, entry/stop-loss/take-profit/risk rules. The only
production code touched is `src/engine/engine.py`'s `IncrementalEngine.process_candle`
/ `to_dict` / `load` (swing-ingestion timing + its persistence), plus a new test file
addition.

## 12. Remaining Known Limitations

1. **Liquidity clustering divergence (unresolved, distinct root cause)**: batch's
   `detect_liquidity_levels` clusters ALL swings across the *entire* dataset by price
   proximity in one retrospective pass (`_cluster_equal_levels`), assigning each
   cluster's *mean* price as the level. `IncrementalLiquidityTracker.ingest_swing`
   instead merges a new swing into an existing level **only if that level is still
   ACTIVE** (never yet swept/archived) — once a level is swept, its price can never
   be re-merged into, and any later swing near that price creates a brand-new level
   instead. On the same 3-week EURUSD window this produced 76 batch levels vs. 858
   live levels. This is a genuine algorithmic difference (not simply the swing-timing
   bug, which is fully fixed), and forcing parity would require deciding whether
   batch's whole-history retrospective clustering is even a *causally valid* target
   for a live system (it implicitly uses swings that occur after a given point in
   time when forming clusters near dataset start) — that decision is explicitly
   out of scope for this task and is recommended as Task 12.4 work.
2. **`registry.refresh()` performance** (Section 9) — unbounded O(n)-per-candle scans
   make multi-month backfills impractically slow. Not fixed here.
3. **Since-inception warm-up / persistent state** (Section 10) — a windowed live
   context still cannot reach batch parity; S3/S4 recall remains 0% in the windowed
   deployment mode. Requires either a very long per-cycle recompute (expensive, given
   #2) or a persistent/checkpointed orchestrator (not built here).
4. GBPUSD/USDJPY parity was not re-verified in this task (time budget; see Section 8).

## 13. Files Changed

- `src/engine/engine.py` — the fix (`_pending_swings` buffering) + persistence
  (`to_dict`/`load`).
- `tests/test_incremental_engine.py` — new regression test.
- `docs/TASK_12_3_INCREMENTAL_STRUCTURE_PARITY_REPORT.md` — this report.

No files deleted. No changes to `reports/live_context_parity/` were committed (all
diagnostic CSVs for this task were produced in the session scratchpad, consistent with
the existing repository convention that such reports may remain outside version
control; the parity numbers are recorded in this document instead).

## 14. Final Acceptance Criteria — Answers

1. **First divergent candle / underlying mechanism**: not a single candle in
   isolation — a systemic one-candle-early swing-activation timing bug, first
   observable divergence in this window's event stream at 2022-12-21 16:45:00 UTC
   (M5 BOS bearish @ 1.06049, batch-only before the fix).
2. **Which primitive diverged first**: structure/BOS-CHoCH ingestion timing (not
   swings themselves, not liquidity's clustering logic, which is a separate issue).
3. **Why**: `IncrementalEngine.process_candle()` ingested newly confirmed swings into
   `structure`/`liquidity` trackers in the same candle that confirmed them, one candle
   earlier than batch's `confirmed_timestamp <= candle.timestamp` semantics.
4. **Bug or intentional?**: Bug — confirmed via full-history byte-identical parity
   after the fix.
5. **Smallest fix**: buffer newly confirmed swings for one candle
   (`self._pending_swings`) before ingestion. ~15 lines including comments.
6. **Point-in-time correctness preserved?**: Yes — if anything, more conservative.
7. **Regression coverage**: 1 new deterministic synthetic test; all 231 pre-existing
   tests still pass (232 total).
8. **Structural parity improvement**: 585/596 (98.2%) event-count match with 21
   symmetric mismatches → **596/596, 0 mismatches** (100%) on the tested window.
9. **S3/S4 recall improvement**: none at 2h/24h windowed lookback (0% → 0%) — expected,
   see Section 8; the bug fixed here is orthogonal to the windowed-lookback problem.
10. **Performance impact**: negligible per-candle overhead from the fix itself;
    separately documented (not fixed) that the engine has an existing O(n)-per-candle
    scaling issue unrelated to this fix.
11. **Is LiveOrchestrator now sufficient?**: For swings/BOS/CHoCH, sufficient *given
    adequate warm-up or persistent state* — neither of which currently exists in the
    windowed GitHub Actions deployment. Not sufficient as currently deployed.
12. **Remaining blockers**: liquidity clustering divergence, registry performance,
    since-inception warm-up/persistence — see Section 12.
13. **Recommended Task 12.4**: (a) resolve the liquidity clustering divergence (decide
    and implement a causally-valid incremental equivalent of batch's global
    equal-level clustering); (b) fix `registry.refresh()`'s unbounded per-candle scans
    (bound the swing/OB/FVG/liquidity lists it reads, e.g. cap `confirmed_swings` scan
    to a trailing window matching the `[-50:]` slice already taken, and prune/index
    the OB/FVG/liquidity trackers' active-object scans); (c) only after (a) and (b),
    revisit whether a persistent/checkpointed `LiveOrchestrator` closes the S3/S4
    recall gap that this task proved is NOT explained by the swing-timing bug fixed
    here.

## 15. Recommended Task 12.4

**Title**: Liquidity Clustering Parity & Incremental Engine Performance.

**Scope**: (1) resolve the liquidity equal-high/low clustering divergence identified
in Section 12.1 — design and implement an incremental clustering algorithm that is
causally valid (uses only swings known up to the current point in time) and reaches
parity with, or a documented, deliberate departure from, batch's retrospective global
clustering; (2) fix the `registry.refresh()` O(n)-per-candle scaling issue so a
multi-month incremental backfill completes in reasonable time, enabling realistic
warm-up experiments; (3) only then, re-attempt the S3/S4 recall measurement with a
much longer incremental warm-up (or a persistent/checkpointed context) to determine
whether recall improves once both remaining architectural issues are addressed.
Explicitly **not** in scope for 12.4: any change to S3/S4 thresholds, ITQS/IOS, the
Decision Engine, or Telegram/GitHub Actions.
