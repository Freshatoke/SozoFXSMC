# Task 7.4 — Performance Profiling & Optimization (Implementation Prompt)

## Context

Task 7.3 imported a real 6.5-year EURUSD M1 dataset (1,992,216 candles, HistData.com) and
proved the platform is **functionally correct** against real market data at every scale tested
(1 month, 3 months — zero exceptions, zero data-integrity failures). It also proved the platform
is **too slow to be practical at multi-year scale**: the full dataset did not complete a single
end-to-end validation campaign run in 141 minutes, at which point it was intentionally
terminated as a completed engineering benchmark (see
`docs/TASK7_3_HISTDATA_VALIDATION_REPORT.md` for full detail and the profiling evidence below).

**Do not add new strategies, new research features, new data providers, or change any
trading/detection logic in this task.** The objective is strictly: make the existing,
already-correct pipeline fast enough to process 10-15 years of M1 data in a practical amount of
time, without changing what it computes.

## Evidence already gathered (do not re-derive from scratch — start here)

Profiling a 3-month real-data slice (91,631 candles, `cProfile`, wall-clock 662.7s) found:

1. **`.iterrows()` in Task 3 strategy scan loops** (`src/strategies/s1_monday_gap.py`,
   `s2_third_bos.py`, and by the same pattern likely `s3_liquidity_sweep.py`,
   `s4_pdh_pdl_sweep.py`, `s5_asian_range_sweep.py` — confirm each) — 344s and 172s cumulative
   respectively for S1/S2 alone on just 3 months of data.
2. **`context.py`'s `latest_choch_asof` / `fresh_order_block_asof` / `structure_state_asof`**
   (`src/strategies/context.py`) — called 50,000+ times on 3 months of data (once per scanned
   M1 candle per strategy per candidate setup). 60-64s of *tottime* each, 124-302s cumulative
   including pandas overhead beneath them.
3. **`detect_structure_events`** (`src/structure/market_structure.py`) — a per-candle Python
   loop using `ts.iloc[i]` (positional pandas `Series` access) instead of a plain numpy
   array/list; 192.9s cumulative for 3 timeframes on 3 months of data.
4. **`detect_swings`** (`src/structure/swings.py`) — similar per-candle Python loop pattern;
   62.9s cumulative.
5. **`compute_weekend_gaps`** (`src/features/reference_levels.py`) — an O(n) scan over the full
   M1 series that does not appear vectorized; 99.5s cumulative for 2 calls.

The profile output also shows tens of millions of `isinstance`/`typing`/`annotationlib` calls,
which is the fingerprint of pandas constructing full `Series` objects per row inside
`.iterrows()`/`.iloc[i]` loops — confirm this diagnosis with your own profiling before assuming
it's the fix, but it is the strongest lead already in hand.

**Measured scaling is worse than linear**: 1-month baseline predicted ~67 minutes for the full
6.5-year dataset by simple linear extrapolation; the actual run exceeded 141 minutes before
being stopped (2x+ worse). This means call-volume-driven costs (proportional to candidate-event
count, which itself grows with dataset size) compound with the per-call pandas overhead above —
both need addressing, not just one.

## Objective

1. **Profile properly first.** Re-run `cProfile` (or `py-spy`/`scalene` if available) on a
   representative multi-month real-data slice using `data/raw/EURUSD_M1_histdata.parquet`
   (already imported, do not re-download/re-import) to get a current, precise picture before
   changing anything. Confirm or revise the hot-path list above with fresh numbers.
2. **Fix the confirmed hot paths without changing behavior.** For each function identified:
   - Replace `.iterrows()`/`.iloc[i]` with vectorized numpy/pandas operations or plain Python
     loops over pre-extracted arrays/lists (the same pattern already used successfully
     elsewhere in this codebase — see `src/strategies/context.py`'s existing `_ob_records`/
     `_structure_records` caches, which were built for exactly this reason but are apparently
     not preventing the pandas overhead seen underneath them; investigate why).
   - Every fix must produce **byte-identical output** to the current implementation on the
     existing test suite (all 138+ tests must still pass unmodified in behavior, only faster)
     — this is a performance task, not a logic change. Add a regression test per fixed function
     comparing old-vs-new output on a fixed synthetic dataset if the existing tests don't
     already pin exact values.
3. **Re-measure at each step.** Don't batch all fixes together and profile once at the end —
   fix the single largest hot path, re-profile, confirm the win, move to the next. This makes
   it possible to attribute the improvement to the right change and catch a fix that
   accidentally makes something else worse.
4. **Establish a scaling benchmark.** Add a `scripts/benchmark_scaling.py` (or extend
   `scripts/benchmark_confluence.py` from Task 2.5, which already exists for a related purpose
   — check it first, reuse its pattern rather than duplicating) that runs the full pipeline on
   1 month / 3 months / 1 year / 3 years of the real HistData dataset and reports wall-clock
   time and memory, so scaling behavior (linear vs. worse) is measured and visible going
   forward, not re-discovered by accident.
5. **Re-attempt the full 6.5-year real-data validation campaign** once the above is done, and
   report the actual completion time. Target: complete in well under 30 minutes (a ~20x
   improvement from the 141+ minutes observed) — treat this as a target to justify against
   measured results, not a number to hit by any means necessary.

## Explicit non-goals (do not do these)

- Do not change BOS/CHoCH/swing/OB/FVG/liquidity definitions or thresholds.
- Do not change strategy entry/exit logic.
- Do not add caching that could introduce staleness/incorrect results across repeated runs —
  any new cache must be provably invalidated correctly (prefer no cache over a risky one).
- Do not attempt this by throwing more parallelism/multiprocessing at the problem as a
  first resort — fix the underlying O(n) anti-patterns first; parallelism is a reasonable
  follow-up once the per-call cost is already reasonable, not a substitute for it.
- Do not touch `src/engine/` (the Task 2.5 incremental engine) unless profiling specifically
  implicates it — it was already optimized once (see `docs/CONFLUENCE_ENGINE.md`) and uses a
  different execution model (candle-by-candle streaming) than the batch `MarketContext` path
  that Task 3's strategies currently use.

## Deliverables

1. Fresh profiling report (before/after per fixed function).
2. The code fixes themselves, each with a regression test proving output is unchanged.
3. `scripts/benchmark_scaling.py` (or the extended existing script) and its output for
   1mo/3mo/1yr/3yr slices of the real dataset.
4. A completed (not terminated) full 6.5-year validation campaign run, with actual wall-clock
   time reported.
5. `docs/PERFORMANCE_OPTIMIZATION.md` documenting what was changed, why, the before/after
   numbers, and any remaining known bottlenecks for a future pass.
6. Updated `docs/TASK7_3_HISTDATA_VALIDATION_REPORT.md`'s readiness assessment (Section 8) to
   reflect the new measured numbers once this task is complete.

## Acceptance criterion

The platform completes a full end-to-end validation campaign (Market Structure → Features →
Strategies → Backtest → Research) over the complete real ~6.5-year, ~2M-candle EURUSD dataset
in a practical amount of time (target: well under 30 minutes), with test-verified identical
output to the current (slow) implementation, and with a documented, evidence-based scaling
benchmark that gives confidence about behavior at 10-15 years before that larger run is
attempted.
