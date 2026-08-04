# Confluence Engine Architecture (Task 2.5)

This document specifies `src/engine/` -- the incremental, event-driven
replacement for request-time confluence recomputation. It does not
replace or modify `src/structure/` or `src/features/`, which remain the
batch/research reference implementation (and are cross-checked for
equivalence in `tests/test_incremental_engine.py`).

## Why this exists

Task 2's `src.features.confluence.build_confluence_snapshot` recomputes
every feature engine from the start of history up to `as_of_index` on
every call -- correct, but O(n) per snapshot, so evaluating one snapshot
per candle over a full run is O(n^2). That's fine for research at a few
thousand candles; it does not scale to years of M1 data across multiple
symbols, or to multiple strategies each wanting a snapshot per candle.

## Architecture

```
New Candle
  -> IncrementalSessionTracker.update()
  -> IncrementalSwingTracker.update()          -- may confirm 0/1/2 swings
       -> IncrementalStructureTracker.ingest_swing()   (per new swing)
       -> IncrementalLiquidityTracker.ingest_swing()   (per new swing)
  -> IncrementalStructureTracker.update()      -- BOS/CHoCH
  -> IncrementalDisplacementTracker.update()   -- (active_run, completed_run)
       -> IncrementalOrderBlockTracker.on_displacement_active()
  -> IncrementalOrderBlockTracker.update()
  -> IncrementalFVGTracker.update()
  -> IncrementalLiquidityTracker.update()      -- sweep checks
  -> IncrementalReferenceLevelTracker.update() -- PDH/PDL/PWH/PWL/weekend gap
  -> IncrementalEngulfingTracker.update()
  -> ActiveObjectRegistry.refresh()
  -> ActiveObjectRegistry.build_confluence_snapshot()  -> ConfluenceUpdated event
```

This fixed order is implemented in `IncrementalEngine.process_candle`
(`src/engine/engine.py`) and matches the task brief's pipeline exactly.
Every step costs work proportional to the number of currently ACTIVE
objects it holds (or O(1)/O(small-constant) for detection), never
proportional to the length of history processed so far -- see
"Per-tracker complexity" below.

## Registry (`src/engine/registry.py`)

`ActiveObjectRegistry` is a read-mostly mirror of tracker state: active
swing highs/lows, active bullish/bearish Order Blocks, active bullish/
bearish FVGs, active liquidity levels, current sessions, current
reference levels (PDH/PDL/PWH/PWL), current market structure state,
current trend state, and the current `ConfluenceSnapshot`. `refresh()` is
called once per candle and is O(active objects), not O(history).

**Nothing is ever deleted.** An object's `current_state`/`state`/
`active_status` field changes (ACTIVE -> PARTIALLY_MITIGATED -> ... as in
`docs/SMC_FEATURE_ENGINE.md`); the `active_*` accessors simply filter for
the still-relevant subset. Full history remains available via each
tracker's `all_order_blocks` / `all_fvgs` / `all_levels` properties.

### A subtle but important correctness rule: snapshots must be immutable copies

Order Block, FVG, and liquidity dicts are mutated in place by their
tracker every candle (e.g. `ob["current_state"] = "FULLY_MITIGATED"`).
If the registry/snapshot held live references to those same dicts, a
snapshot handed out for candle 100 would silently change its contents
once candle 150 mitigated the same object -- a real bug found and fixed
during this task (`test_past_snapshots_are_immutable_once_produced`).
Every `active_*()` accessor (`IncrementalOrderBlockTracker.active_order_blocks`,
`IncrementalFVGTracker.active_fvgs`, `IncrementalLiquidityTracker.active_levels`)
therefore returns **shallow copies**, and `ConfluenceSnapshot.to_dict()`
also uses a shallow copy (`vars(self)`), not `dataclasses.asdict()` --
see the performance note below for why that distinction also matters a
great deal.

## Event Bus (`src/engine/event_bus.py`)

A plain synchronous publish/subscribe registry plus an append-only event
log (`EventBus.log`), with event types:

```
SwingConfirmed, BullishBOS, BearishBOS, BullishCHoCH, BearishCHoCH,
OrderBlockCreated, OrderBlockMitigated, OrderBlockInvalidated,
FVGCreated, FVGMitigated, LiquidityCreated, LiquiditySwept,
SessionStarted, SessionEnded, ReferenceLevelUpdated, ConfluenceUpdated
```

Every event carries a monotonically increasing `sequence` number assigned
at publish time -- this is what `tests/test_incremental_engine.py::test_event_ordering_matches_pipeline_sequence`
checks (sequences are never reordered, never duplicated). Future
strategies subscribe to the events they care about instead of polling
every tracker on every candle.

## Order Block creation timing: a design decision unique to streaming

The Task 2 batch Order Block engine can afford to wait until a
displacement RUN COMPLETES (a candle arrives that breaks the run) before
creating the OB, because it already has the whole history in hand. A
streaming engine cannot: if the stream ends mid-run (e.g. "now" is the
last known candle), that "closing" candle may simply never arrive within
the current session, and an OB that only ever gets created retroactively
is useless to a live strategy.

`IncrementalDisplacementTracker.update()` therefore returns
`(active_run, completed_run)`: `active_run` describes the run as of THIS
candle every candle it is in progress (including its very first
qualifying candle), and `IncrementalOrderBlockTracker.on_displacement_active`
creates the OB on the run's first candle, then just refreshes the same
object's `displacement_reference` in place on every subsequent candle of
the same run (keyed by the run's `start_index` -- one OB per run, never
duplicated). `completed_run` is retained only for audit purposes.

This also means mitigation checks must skip every candle still inside
the SAME run that created the OB (`candle.index <= ob["displacement_reference"]["end_index"]`),
not just the OB's own creation candle -- otherwise, exactly as with the
batch engine's own fix in Task 2, the impulsive leg's own first candle
(whose `open` sits inside the OB zone by construction of continuous OHLC
data) would trivially register as an immediate "touch" on every OB ever
created.

## Per-tracker complexity

| Tracker | Per-candle cost | Notes |
|---|---|---|
| Swings | O(1) | fixed-size `left+right+1` rolling window |
| Structure (BOS/CHoCH) | O(1) | single active high/low level, as in the batch engine |
| Displacement | O(1) | rolling ATR/avg-body via `deque` |
| Order Blocks | O(active OBs) | dict-keyed storage, O(1) lookup by id |
| FVG | O(active FVGs) | dict-keyed storage, O(1) lookup by id |
| Liquidity | O(active levels) for sweep checks; O(active levels of the same side) for clustering, only on new swings | dict-keyed storage |
| Sessions | O(number of configured sessions) = O(1) | reactive to time-of-day, no date iteration |
| Reference levels | O(1) amortized; O(open weekend gaps) for gap fill tracking | weekend gaps are rare (~1/week) |
| Engulfing | O(1) | only needs the previous candle |
| Registry refresh | O(active objects across all trackers) | never O(history) |
| Confluence snapshot build | O(active objects) | see the `to_dict()` note below |

**All dict-keyed storage matters more than it looks.** An earlier draft
of this engine stored objects in plain lists and looked them up with
`next(x for x in list if x["id"] == id)` inside per-candle O(active)
loops. That silently turns "O(active)" into "O(active) x O(all objects
ever created)" -- i.e. still quadratic overall, just with a smaller
constant than the batch approach. This was caught by profiling (see
Benchmarking below), not by inspection -- a useful lesson for future work
in this codebase: an "O(active)" loop is only actually O(active) if
everything inside it is O(1).

## Confluence Snapshot

`ConfluenceSnapshot` (a dataclass) fields: `timestamp`, `symbol`,
`timeframe`, `market_state`, `trend`, `active_order_blocks`, `active_fvgs`,
`active_liquidity`, `current_session`, `asian_high`, `asian_low`, `pdh`,
`pdl`, `weekend_gap`, `engulfing_signal`, `displacement_signal`. Built
once per candle by `ActiveObjectRegistry.build_confluence_snapshot` and
published as the `ConfluenceUpdated` event payload -- this is the single
source of truth future strategies should read.

`to_dict()` is a **shallow** copy (`vars(self)`), not
`dataclasses.asdict()`. `asdict` recursively deep-copies every nested
value and falls back to `copy.deepcopy` for anything that isn't a plain
dataclass/list/dict/tuple -- including every `pd.Timestamp` inside every
active Order Block/FVG/liquidity dict. Called once per candle, that
turned out to be the dominant cost of the entire pipeline during
benchmarking (profiling showed >85% of total runtime in
`copy.deepcopy`/`_reconstruct` calls originating from `asdict`). Since
every list a snapshot holds is already a fresh, non-shared copy (see the
registry section above), a shallow copy is correct and roughly 5-10x
faster in practice.

## Persistence / Recovery mechanism

`IncrementalEngine.save(path)` serializes every tracker's internal state
(rolling windows, active-id sets, pending-invalidation/-archive deadlines,
full object histories, event sequence counters) to JSON.
`IncrementalEngine.load(path)` reconstructs a new engine and restores each
tracker via its `restore()` method, then refreshes the registry so it is
immediately consistent.

Candle indices are a running count (`IncrementalEngine.candles_processed`)
carried across save/load boundaries -- `process_dataframe` continues
indexing from `self.candles_processed`, not from 0, on every call. This
was a real bug caught during development: resetting the index per call
silently corrupted every index-based comparison (OB/FVG/liquidity
creation-index checks) after a resume. `tests/test_incremental_engine.py::test_restart_recovery_matches_continuous_run`
verifies that processing a stream in two halves with a save/load in
between produces byte-identical tracker state to processing it in one
continuous run.

Timestamps and dates are round-tripped through a small custom JSON
encoder/decoder (`_json_default` / `_json_object_hook` in `registry.py`,
reused by `engine.py`) since `pd.Timestamp` and `datetime.date` are not
natively JSON-serializable.

## Performance characteristics / Benchmarking

`scripts/benchmark_confluence.py` compares:
- **Old**: `src.features.confluence.build_confluence_snapshot` called
  once per candle over a run of `--num-candles` candles (O(n^2) total).
- **New**: `IncrementalEngine.process_dataframe` over the same run, plus
  a separate larger `--incremental-only-candles` run to show its O(n)
  scaling directly (the old approach is not run at that size -- it would
  take prohibitively long, which is exactly the point).

Run it yourself:
```bash
python scripts/benchmark_confluence.py --num-candles 150 --incremental-only-candles 100000
```

See the Final Report (delivered alongside this task) for the actual
measured numbers from this environment.

## Known limitations

- The engine assumes a single (symbol, timeframe) stream per
  `IncrementalEngine` instance; running multiple symbols/timeframes means
  running multiple instances (no shared state is assumed or required
  between them, which is itself intentional -- it keeps each instance's
  complexity bounds independent of how many symbols a strategy runner
  manages).
- `IncrementalLiquidityTracker`'s equal-level clustering only compares a
  new swing against currently ACTIVE levels of the same side -- this
  matches the batch engine's behavior but means a swept/archived level
  can never re-merge with a later swing at the same price (a new level
  is created instead). This is a deliberate simplification, not a bug:
  once a liquidity pool has been used (swept), a later touch at the same
  price is genuinely a *new* pool from a market-structure standpoint.
- Order Block invalidation only looks for an opposing BOS/CHoCH within
  `invalidation_lookahead` candles of full mitigation, exactly as in the
  batch engine -- see `docs/SMC_FEATURE_ENGINE.md` for the full rationale
  and its own known limitations (quality-score factors, Asian-session
  definition, etc.), all of which still apply here unchanged.
- Weekend-gap fill tracking assumes at most a handful of concurrently
  open gaps (realistic for FX, where gaps close within days), and its
  linear scan (`next(g for g in weekend_gaps if ...)`) is intentionally
  not optimized further for that reason.
