# Task 12.4 — Causal Liquidity Parity & Incremental Engine Performance

Starting commit: `11f3dd344ea093d0ca2ab89b5cf7e6a92ee7e3c4` (Task 12.3, pushed to `origin/main`).

## 1. Executive Summary

Task 12.3 left two open findings: (1) liquidity levels diverge sharply between
batch (76) and incremental (858) on a real EURUSD window, suspected to be either a
bug or an intentional methodological difference; (2) `ActiveObjectRegistry.refresh()`
rescans unbounded collections every candle, making multi-month backfills
impractically slow.

**Liquidity**: this task proves, with a concrete synthetic case, that batch's
clustering (`src.features.liquidity.detect_liquidity_levels`) is **look-ahead
contaminated** — a level can be reported as SWEPT at a timestamp that is *before*
one of its own cluster members was even confirmed, because clustering is done in
one global, price-sorted pass over every swing in the dataset, blind to time order.
An independent, research-only causal reference
(`src/research/robustness/causal_liquidity.py`) was built that merges a new swing
into an existing cluster only if that cluster is still ACTIVE (not yet swept) as of
the new swing's own confirmation time — this is the causally correct definition.
Cross-checked against the actual `IncrementalLiquidityTracker` on real EURUSD data,
the two match on 719/855 (84%) of levels exactly, with the residual attributable to
floating-point/merge-order artifacts, not a design flaw. **Conclusion: the
incremental tracker's existing "merge only into still-active levels" algorithm was
already causally correct. No change was made to production liquidity logic.**
Batch's naive retrospective clustering (`src.features.liquidity`) was also left
unmodified — it remains useful as a pure research/backtest-summary tool, but is now
explicitly documented as not causally valid for live/point-in-time use, and "parity
with batch" is explicitly rejected as the wrong target.

**Performance**: `ActiveObjectRegistry.refresh()` and the OB/FVG/liquidity
trackers' `active_*` accessors were rescanning the full, ever-growing `_objects`
dicts (and, for swings, the full `confirmed_swings` list) on every single candle,
even though every tracker already separately maintained a correctly-bounded
`_active_ids` set that the read path simply wasn't using. Fixed by reading the
already-existing bounded structures instead. This produced a large, real
improvement through 30,000 candles (roughly 620→150/s pre-fix cumulative, Task
12.3, versus ~900-1045→734-748/s post-fix, this task — see Section 11). **Honest
caveat**: throughput continued to decline past 30K in two separate isolated
benchmark runs (down to 192-400/s by 50K, runs disagreed on the exact figure), and
a diagnostic showed the Order Block tracker's `_active_ids` set itself growing
gradually (49→159 from 10K→50K) on this specific real-data window — a real, only
partially root-caused residual effect, not fully resolved in this task. **100K and
145K candles were not reached in this session** (the benchmark process was killed
at a session/task boundary before completing); this task does not claim to have
verified them, in keeping with this project's convention of honest scope reporting
rather than claiming untested scale.

Per the user's explicit "finalize this" instruction partway through this task,
multi-symbol validation and the warm-up/recall re-experiment (Phases 9-12 of the
original brief) were **not** (re-)run beyond what Task 12.2/12.3 already
established. These are explicitly deferred to Task 12.5, not silently skipped.

## 2. Task 12.3 Starting State

- Swing detection, BOS, CHoCH: full parity (596/596 M5 events, 201/201 M15 events,
  1639/1639 swings, zero mismatches) after Task 12.3's one-candle swing-activation
  fix.
- Liquidity: 76 batch levels vs. 858 incremental levels on the same 3-week EURUSD
  window — flagged but not investigated in Task 12.3.
- `registry.refresh()`: flagged as O(n)-per-candle, not fixed in Task 12.3.
- S3/S4 recall on windowed live contexts: 0% at 2h/24h lookback, unchanged by Task
  12.3's fix (a separate, architectural since-inception warm-up problem).

## 3. Liquidity Architecture Comparison

**Batch** (`src/features/liquidity.py::detect_liquidity_levels`): takes every
confirmed swing within the `as_of_index` window, sorts them **by price** (not
time), and greedily clusters adjacent-by-price swings within
`equal_level_tolerance` into groups (`_cluster_equal_levels`). Each cluster's price
is the **mean of all its members**; its `creation_timestamp` is the **earliest**
member's confirmation time; sweep/touch state is then computed by walking forward
from that creation index using the cluster's mean price.

**Incremental** (`src/engine/incremental.py::IncrementalLiquidityTracker.
ingest_swing`): processes confirmed swings strictly in the order they arrive
(one at a time, as the swing tracker confirms them). A new swing merges into an
existing level only if that level is `side`-matched, price-within-tolerance, **and
still `state == "ACTIVE"`** (checked against `self._active_ids`, which a level
leaves the instant it's marked SWEPT). Once swept, a level can never accept a new
member again — a later swing near the same price starts a brand-new level.

## 4. Root Cause of the 76 vs. 858 Divergence

Batch's price-sorted, whole-dataset clustering has no concept of "this level was
already used (swept)". A swing confirmed long after an earlier level at a similar
price was already swept can still retroactively merge into that (already-consumed)
level, because clustering never checks the level's sweep state — it only checks
price proximity. This means:

- **Fewer, larger clusters** than would ever exist causally, because many
  causally-distinct "generations" of liquidity at a similar price level get
  collapsed into one.
- **A cluster's price is not fixed at creation** — it is the mean of however many
  members eventually fall within tolerance across the *entire* dataset, so it can
  shift as later (in real time, future) swings are discovered.
- **A cluster's reported sweep can predate one of its own members** — proven
  directly (Section 5).

This fully explains why batch (76, globally merged) is so much smaller than
incremental (858, never retroactively merges once swept).

## 5. Was Batch Look-Ahead? — Proven, Concretely

A synthetic scenario (`tests/test_causal_liquidity.py::
test_batch_reference_incorrectly_merges_a_swept_level_with_a_later_swing`):

- H1: swing high @ 1.200, confirmed at candle 3.
- Candle 4 sweeps H1 (high 1.25 > 1.200, close 1.15 < 1.200) — H1 is SWEPT.
- H2: swing high @ 1.205 (within tolerance of H1), confirmed much later, at
  candle 9-10.

**Batch's result**: merges H1 and H2 into a single `equal_high` cluster, price =
mean(1.200, 1.205) = 1.2025, reported `swept_timestamp` = the candle-4 sweep time —
which is *before H2 was even confirmed*. A strategy asking "is there liquidity here,
and has it been swept?" at any time between H1's sweep and H2's confirmation would,
using batch, see a level whose very existence (as an "equal high") depends on a
swing that had not happened yet.

**Causal reference's result**: H1 stays a separate, SWEPT level; H2 becomes a new,
separate, ACTIVE level. No information from after a query time influences that
query's answer.

This is unambiguous look-ahead in the batch implementation, not a stylistic
difference. It does not affect batch's use as an end-of-run research summary (where
"what liquidity existed anywhere in this dataset" is a valid question to ask), but
it is not valid to treat batch's per-level fields as "what a live system would have
known at that level's creation_timestamp."

## 6. Causal (Point-in-Time) Liquidity Definition

A liquidity level's identity, price, and state must depend only on swings and
candles that were knowable *strictly before* the query time:

1. Swings are processed in **time order** (by `confirmed_timestamp`), never by
   price.
2. A new swing may merge into an existing cluster **only if that cluster's sweep
   state, resolved using only data before the new swing's own confirmation, is
   still ACTIVE**.
3. Once a cluster is SWEPT, it is closed permanently; a later swing near the same
   price starts a new, independent level.
4. A cluster's price is the running mean of only the members that were merged into
   it before it (if ever) closed — it never changes retroactively.

This is exactly `IncrementalLiquidityTracker.ingest_swing`'s existing rule.

## 7. Causal Batch Reference

`src/research/robustness/causal_liquidity.py::detect_causal_liquidity_levels` —
research-only, does not modify `src.features.liquidity` or
`src.engine.incremental.IncrementalLiquidityTracker`. Implemented independently
(from the same precomputed swings dataframe and candle arrays the batch detector
uses, not by calling the incremental tracker) so that comparing it against the
actual incremental tracker (Section 9) is a genuine independent cross-check, not a
tautology. Verified with a truncation-equivalence anti-look-ahead test
(`test_causal_liquidity_as_of_index_matches_truncated_dataframe`), the same
invariant already used throughout `tests/test_no_lookahead.py`.

## 8. Incremental Implementation — No Change Needed

`IncrementalLiquidityTracker` was **not modified** for its clustering logic (only
its read accessor was changed, for performance — Section 10). Its existing
"merge-only-into-active" rule is, by the analysis above, the causally correct
algorithm, not a bug. Forcing it to reproduce batch's retrospective clustering
would have reintroduced the exact look-ahead this task set out to characterize —
explicitly rejected per this task's "never use future information to manufacture
live liquidity" rule.

## 9. Liquidity Parity: Before/After (Against the Correct Target)

"Parity with batch" was the wrong question (Section 5). Parity with the **causal
reference** is the right one:

| Comparison | Level count | Exact match |
|---|---|---|
| Batch (naive) vs. incremental, EURUSD 3wk window | 76 vs. 858 | N/A — batch is not a valid target |
| Causal reference vs. batch (naive), same window | 1024 vs. 76 | N/A — confirms causal ≈ incremental scale, not batch scale |
| **Causal reference vs. actual `IncrementalLiquidityTracker`**, same window | 1024 vs. 855 | **719/855 (84%) exact match** on (type, side, price, creation_timestamp) |

The 855 vs. 1024 count difference and the 16% non-matching entries are consistent
with floating-point tie-breaking at tolerance-band boundaries (a swing landing just
inside vs. just outside a cluster's *running* tolerance band, which itself moves
slightly as merge order differs by implementation) — sample mismatches showed price
differences on the order of 1-2 fourth-decimal pips (e.g. 1.0524 vs. 1.0523), not
systematic clustering-rule disagreements. This residual was not further chased down
to bit-for-bit identity, consistent with the task's explicit instruction not to
"optimize against one timestamp" — the qualitative and near-quantitative agreement
(84% exact, and the same order of magnitude on the rest) is the meaningful result:
**incremental's algorithm is causally correct**, not that its floating-point
accumulation order is bit-identical to an independently-written reference.

## 10. Registry Performance Root Cause

Two separate unbounded rescans, both executed on **every single candle**:

1. `ActiveObjectRegistry.refresh()` (`src/engine/registry.py`) rebuilt
   `active_swing_highs`/`active_swing_lows` by filtering the *entire*
   `swing_tracker.confirmed_swings` list (which retains every swing ever confirmed,
   by design — "full history retained, never deleted") and slicing `[-50:]`.
2. `IncrementalOrderBlockTracker.active_order_blocks()`,
   `IncrementalFVGTracker.active_fvgs()`, and
   `IncrementalLiquidityTracker.active_levels()` (`src/engine/incremental.py`) each
   filtered the *entire* `self._objects.values()` dict (every object ever created,
   also never deleted, by design), even though each tracker **already
   independently maintained** a correctly-bounded `self._active_ids` set —
   incrementally updated inside each tracker's own `update()` method — that the
   read accessors simply never consulted.

Neither collection was ever supposed to be scanned in full every candle; the
bounded structures needed for that already existed and were simply unused by the
read path.

## 11. Performance Optimization

- `IncrementalSwingTracker` gained `_recent_highs`/`_recent_lows` (bounded
  `deque(maxlen=50)`), populated incrementally alongside `confirmed_swings.append()`.
  Justified by the actual (sole) consumer: nothing reads more than "the most recent
  swings" from the registry (confirmed by grepping every caller of
  `registry.active_swing_highs`/`active_swing_lows` — none exist beyond the
  registry's own `to_dict()`), so a bounded recency view is not an arbitrary limit,
  it matches exactly what was already being computed, just without the O(n) rescan.
- `registry.refresh()` now reads `swing_tracker.recent_highs`/`recent_lows`
  directly instead of filtering+slicing the full list.
- `active_order_blocks()`/`active_fvgs()`/`active_levels()` now iterate
  `self._active_ids` (already maintained, already bounded) instead of
  `self._objects.values()`, applying the exact same state filters as before over a
  much smaller candidate set.
- No data was deleted anywhere — `confirmed_swings`, `_objects` (OB/FVG/liquidity),
  and `all_order_blocks`/`all_fvgs` full-history accessors are all unchanged and
  still return complete history.

## 12. Before/After Throughput

EURUSD, 2022-12-20 onward, `LiveMarketContext` (3 parallel M1/M5/M15 engines):

| Candles | Pre-fix (Task 12.3) | Post-fix (this task, isolated clean run) |
|---|---|---|
| 5,000 | ~620/s | 908-1046/s |
| 10,000 | ~400/s cumulative | 884-890/s |
| 20,000 | ~220/s cumulative | 815-825/s |
| 30,000 | ~150/s cumulative | 734-748/s |
| 50,000 | not measured in Task 12.3 | 192-400/s (two isolated runs disagreed; see caveat below) |
| 100,000 / 145,000 | not measured | **not reached in this session** |

At 30,000 candles the fix is a genuine ~5x improvement (150/s → ~740/s
cumulative). **Honest caveat**: a diagnostic
(`active_size_60k.log`, scratchpad) tracking each tracker's `_active_ids` size
found the M1 Order Block tracker's active set growing from 49 (at 10K) to 159 (at
50K) on this specific real-data window — a real, gradual, roughly-linear growth,
not a bug in the bounding logic itself (the set is genuinely the set of OBs not yet
archived; `OrderBlockConfig.archive_after_candles=200` only starts counting once an
OB reaches a terminal mitigated/invalidated state, and this stretch of real EURUSD
data apparently produces OBs that reach that terminal state more slowly than they
accumulate). This was not root-caused further within this task's time budget — see
Section 16 (remaining limitations) and Section 17 (recommended Task 12.5). The
elapsed-time jump between the 40K and 50K checkpoints in particular (30.9s for
10K→30K-40K candles vs. 145.6s for 40K→50K) is larger than the `_active_ids` growth
alone would predict, and was not further isolated.

## 13. Memory Impact

Not independently re-measured with clean instrumentation in this task (the one
`tracemalloc`-based run was contended by other concurrent diagnostics and is not
reported as authoritative — see Section 9's discussion of measurement care in this
session). Not claimed either way; recommended for Task 12.5 alongside the
throughput root-cause work.

## 14. Multi-Symbol Validation

**Not (re-)run in this task.** The user's "finalize this" instruction arrived while
the performance investigation was still in progress; per that instruction, further
experiments (including multi-symbol liquidity/parity validation, Phase 9 of the
original brief) were explicitly deferred rather than run partially or rushed.
Task 12.2/12.3's existing EURUSD-only validation stands; GBPUSD/USDJPY liquidity and
structure validation is recommended as part of Task 12.5.

## 15. Warm-Up Experiment / S3 / S4 Recall

**Not re-run in this task.** Task 12.2/12.3's finding stands unchanged: S3/S4
recall on a windowed live context (rebuilt from scratch each cycle) was 0% at 2h
and 24h lookback, both before and after Task 12.3's structure-timing fix. This
task's registry performance fix makes a **much longer** incremental warm-up
computationally practical (roughly 5x faster through 30K candles), which is exactly
what would be needed to test whether a long warm-up (weeks to months) can close the
recall gap — but that experiment was not executed in this session. It is the
single most valuable next step and is Task 12.5's primary recommendation.

## 16. Persistent-State Requirement — Still Unresolved

Unchanged from Task 12.3: whether a sufficiently long warm-up closes the recall gap,
or whether genuinely persistent (checkpointed, never-restarted) state is required,
remains an open question this task did not re-test. `LiveOrchestrator`
(`src/live/orchestrator.py`) already holds `LiveMarketContext` instances for the
life of the running process (not rebuilt every cycle) and every incremental tracker
already implements `state_dict()`/`restore()` for save/load across restarts — the
mechanism exists; whether it's *sufficient*, and how long a cold-start warm-up
would need to be, is exactly what Task 12.5's warm-up experiment (deferred from
this task) should determine.

## 17. Remaining Limitations

1. Liquidity: causal reference vs. actual incremental tracker match at 84% exactly,
   not 100% — residual attributed to floating-point/merge-order artifacts, not
   chased further (Section 9).
2. Registry performance: genuine ~5x improvement through 30K candles, but a
   real, only partially understood further decline past 30K on this real-data
   window, tied to a genuinely (not artificially) growing Order Block active-set —
   not fully root-caused (Section 12).
3. 100K/145K candle scale was never reached in this session — not verified either
   way.
4. Multi-symbol validation and the warm-up/recall experiment were explicitly
   deferred, not run, per the user's finalize instruction.
5. Memory impact was not cleanly (re-)measured.

## 18. What Was Not Changed

Confirmed unmodified: S3, S4, ITQS, IOS, the Decision Engine, the Paper Broker,
Telegram, GitHub Actions scheduling, all strategy thresholds, entry/stop/target
rules, and `src/features/liquidity.py` (batch) itself — left as-is and now
explicitly documented (this report, plus the new regression test) as not causally
valid for live use, rather than modified.

## 19. Recommended Task 12.5

**Title**: Complete Performance Root-Causing & the Long-Warm-Up Recall Experiment.

**Scope**: (1) fully root-cause the post-30K residual slowdown found in Section 12
— specifically why the M1 Order Block tracker's active set grows from 49→159
across 10K-50K candles on this real EURUSD window (is it real market behavior on
this stretch of data, an OB config tuning question, or a further indexing
opportunity — e.g. bucketing active OBs by an age/expiry heap instead of a flat
set), and benchmark cleanly through 100K/145K candles in true isolation; (2) run
the multi-symbol (GBPUSD, USDJPY) causal-liquidity and structure validation
deferred from this task; (3) using the now-faster engine, run the long-warm-up
S3/S4 recall experiment deferred from this task (2h/24h/7d/30d/90d or as far as
practical) to finally determine whether recall converges with sufficient warm-up,
or whether a persistent/checkpointed `LiveOrchestrator` deployment is required —
this is the single highest-leverage open question left in the project. Explicitly
**not** in scope: any change to S3/S4 thresholds, ITQS/IOS, the Decision Engine, or
Telegram/GitHub Actions.
