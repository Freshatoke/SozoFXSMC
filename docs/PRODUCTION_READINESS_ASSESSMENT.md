# Production Readiness Assessment

Updated after Task 7.4 (Performance Optimization & Scalability). See
`docs/PERFORMANCE_OPTIMIZATION.md` for the full optimization log and
`docs/TASK7_3_HISTDATA_VALIDATION_REPORT.md` /
`docs/TASK7_ENGINEERING_REVIEW.md` for the correctness/architecture
review this assessment builds on.

## Summary

| dimension | status |
| --- | --- |
| Correctness (trading logic) | ✅ Unchanged since Task 7.3 — Task 7.4 made zero trading-logic modifications, verified via golden-snapshot equality at every step |
| Test coverage | ✅ 175/175 tests passing |
| Real-data validation | ✅ Full 6.5-year EURUSD M1 campaign (1,992,216 candles, 5,632 trades) completes successfully end-to-end |
| Performance | ✅ 11–13x faster on small/medium slices; full campaign now completes in ~33 minutes (previously killed at 141 min, incomplete) |
| Scalability | ⚠️ Good — O(n²)-class bugs eliminated; residual scaling is memory-bound (peak RSS ~1.2–1.5GB at 6.5yr), not algorithmic. Multi-symbol / 10-15yr research is practical but should be planned with memory in mind (see Recommendations) |
| Progress observability | ✅ Live stage/%/ETA/memory/throughput reporting for long-running commands |
| Single-symbol readiness | ✅ Ready |
| Multi-symbol / multi-year research readiness | ✅ Ready, with the memory caveat above |

## What changed since the last assessment (Task 7.3)

Task 7.3 established correctness against real market data but flagged
runtime as impractical (141+ minutes for one symbol's full history, not
even completing). Task 7.4 was a pure performance-engineering pass:

- Eliminated multiple O(n²)-class bugs (linear-scan-from-zero lookups
  whose call count also grew with the dataset, and full-dataframe
  boolean filters executed once per trade/signal).
- Verified, at every single step, that outputs are byte-identical to
  the pre-optimization baseline (golden-snapshot comparison) and that
  all 175 tests still pass — no behavioral risk was introduced.
- Added a scaling benchmark utility and progress instrumentation as
  permanent tooling (not one-off scripts), so future performance work
  and long research runs both have reusable infrastructure.
- Re-ran the full 6.5-year campaign to completion: it now finishes in
  32m51s and produces every expected report artifact.

## Residual risks / known limitations

1. **Peak memory grows with dataset size** (~259MB at 3 months → ~1.5GB
   at 6.5 years for a single symbol). This is now the dominant scaling
   factor, not per-call algorithmic complexity. Running many symbols
   concurrently in one process, or extending to 10-15 years, should be
   tested for memory headroom before being treated as routine.
2. **One caught-and-reverted regression during this task** (documented
   in `PERFORMANCE_OPTIMIZATION.md` §2e) is worth internalizing as a
   process lesson: an optimization that materializes a lazy structure
   eagerly can pass every correctness test while making performance
   *worse* at scale, because production-scale inputs (a 2M-row dataset)
   exercise code paths that small test/profiling inputs (1mo/3mo slices)
   don't. **Recommendation**: any future performance work on this
   codebase should include at least one profiling/benchmark pass at or
   near production scale before being considered complete, not just at
   the small scale convenient for fast iteration.
3. **Single symbol validated end-to-end** (EURUSD). The strategies,
   backtest engine, and now the performance characteristics are
   symbol-agnostic by construction (no symbol-specific branching in the
   hot paths touched by this task), but a multi-symbol campaign has not
   itself been run to completion yet.

## Recommendations for next steps

- Before scaling to multi-symbol campaigns, run one 2-symbol campaign
  end-to-end and confirm peak memory scales roughly linearly with
  symbol count (expected, since each symbol's MarketContext/caches are
  independent) rather than compounding unexpectedly.
- If 10-15 year single-symbol or multi-symbol runs become routine,
  revisit the memory-growth limitation above — likely lever is
  processing/exporting in chronological chunks rather than holding the
  full dataset and all derived caches in memory simultaneously.
- Keep `scripts/benchmark_scaling.py` and `scripts/golden_snapshot.py`
  in the regular workflow for any future change to `src/backtest/`,
  `src/strategies/`, or `src/structure/` — they are now the fastest way
  to catch both correctness and performance regressions in this part of
  the codebase.
