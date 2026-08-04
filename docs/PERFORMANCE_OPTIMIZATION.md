# Task 7.4 — Performance Optimization & Scalability

## Objective

Task 7.3 proved the platform functionally correct against real HistData
EURUSD M1 data (2020–2026, 1,992,216 candles), but the first full
6.5-year validation campaign was killed after ~141 minutes as an
engineering benchmark — correct, but too slow to be practical for
repeated research runs or multi-symbol / multi-year expansion.

This task's mandate: **identical outputs, dramatically faster
execution.** No trading logic (BOS/CHoCH, swings, Order Blocks, FVGs,
liquidity, entry/exit rules, backtest rules) was touched. Every
optimization below is a pure computational-efficiency change, verified
behavior-identical via golden-snapshot comparison and the full test
suite before being accepted.

---

## 1. Baseline

Measured with `scripts/benchmark_scaling.py` (built for this task) on
real HistData EURUSD M1 data, before any optimization:

| size | candles | wall time | candles/sec |
| --- | --- | --- | --- |
| 1mo | 31,653 | 159.1s | 198.9 |
| 3mo | 93,383 | 542.8s | 172.1 |

**candles/sec DROPPING as the dataset grows is the key signal**: it
means the pipeline was worse than linear — several functions were
re-scanning growing data structures from scratch on every call.

Top hotspots from `cProfile` (3-month profile, by cumulative time):

| function | calls | tottime | cumtime |
| --- | --- | --- | --- |
| `context.py::latest_choch_asof` | 92,950 | 74.9s | 234.2s |
| `context.py::fresh_order_block_asof` | 64,915 | 44.1s | 87.9s |
| pandas `indexing.py::__getitem__` | 863,221 | 6.0s | 240.3s |
| pandas `datetimelike.py::__getitem__` | 1,054,787 | 28.6s | 157.0s |
| `DataFrame.iterrows` | 162,095 | 1.7s | 64.8s |
| `market_structure.py::detect_structure_events` | 3 | — | 192.9s |
| `reference_levels.py::compute_weekend_gaps` | 2 | — | 99.5s |

Root cause pattern, repeated across the codebase: **linear-scan-from-
index-0 lookup functions called once per M1 candle**, where the list
being scanned also grows with the dataset — an O(n²)-class bug hiding
inside what looked like O(n) code.

---

## 2. Optimization Log (one bottleneck at a time)

Every fix below was verified against `scripts/golden_snapshot.py`
(byte-identical signals/trades/metrics vs. the pre-optimization
baseline) and the full test suite (175 tests) before being accepted.

### 2a — `.iterrows()` → `.itertuples()` in strategy scans
**Files**: `src/strategies/s1_monday_gap.py` … `s5_asian_range_sweep.py`
Each strategy's per-M1-candle scan loop used `.iterrows()` (constructs a
full pandas Series per row). Switched to `.itertuples(index=False)`
(cheap namedtuples). Outer, much-smaller frames (weekend gaps, swept
levels, PDH/PDL rows, Asian sessions — called dozens to hundreds of
times, not tens of thousands) were deliberately left untouched.

### 2b — `.iloc[i]` / `.loc[]` in structure detection loops
**Files**: `src/structure/market_structure.py`, `src/structure/swings.py`
`detect_structure_events`'s per-candle walk read `ts.iloc[i]` and
`swings_sorted.loc[swing_ptr]` — pandas positional/label access through
full indexing machinery, once per candle. Replaced with plain Python
lists built once (`ts.tolist()`, `df.to_dict("records")`) and indexed
positionally. `swings.py`'s fractal-detection loop had the same
`ts.iloc[i]` pattern, fixed the same way.

### 2c — `context.py` as-of lookup functions (bisect)
**File**: `src/strategies/context.py`
`structure_state_asof`, `latest_choch_asof`, `fresh_order_block_asof`
each re-scanned their cached record list from index 0 on *every* call —
this was the single largest hotspot (309s combined cumtime on the
baseline profile). All three records lists are chronologically sorted by
construction; rewrote all three to use `bisect.bisect_right(records,
timestamp, key=...)` to find the cutoff in O(log n), then scan backward
from there. Sortedness was verified empirically (not assumed) before
implementing.

### 2d — `compute_weekend_gaps` vectorization
**File**: `src/features/reference_levels.py`
Scanned every candle in the dataset (`range(1, last_index+1)`) with two
`.iloc[]` lookups each, for a condition (Friday-close-to-weekend-gap)
true on roughly 1 candle in 5,000. Replaced with a single vectorized
`diff()`/`weekday()` pass to find candidate gap boundaries, then only
iterates the actual candidates (roughly one per week instead of every
candle).

**Combined effect of 2a–2d** (1mo / 3mo, clean re-profile):
wall time 159.1s→73.7s / 542.8s→129.5s — first point where candles/sec
started *increasing* with dataset size (198.9→230.4 / 172.1→243.6),
confirming the O(n²) pattern was broken.

### 2e — Remaining hotspots pass
Five further fixes, each verified independently:

1. **`engine.py::simulate_trade` — `history_so_far`**: computed a full
   `m1[m1.timestamp <= candle.timestamp]` filter on *every candle of
   every open trade*, but the result is only ever read by
   `check_trailing_stop`'s `"atr"` branch — under the default config
   (`trailing_method=None`) it was 100% wasted work. Now only computed
   when `trailing_method == "atr"`.
2. **`context.py::fresh_order_block_asof` — touch-null caching**: the
   backward scan called `pd.isna(touch)` on every record visited.
   Precomputed once per record when the OB cache is built.
3. **`engine.py::simulate_trade` — per-candle iteration**: converting
   the whole remaining window to a list/dict upfront (tried first, then
   reverted — see below) vs. `window.itertuples(index=False)`, which is
   lazy and never materializes candles the loop doesn't reach.
4. **`context.py::session_active_asof`**: linear-scanned *all* session
   records (every session name interleaved) from index 0 on every call.
   Sessions of one name never overlap and are chronologically ordered
   per name, so grouping by name once and bisecting within that list
   finds the (at most one) candidate in O(log n).
5. **`stop_loss.py::_true_range_atr`, `entry.py`, `engine.py` — full-
   dataframe filters**: `m1[m1["timestamp"] <= x]` / `m1[m1["timestamp"]
   > x]` boolean filters over the *entire* m1 frame, executed once per
   trade or per signal. At small scale (1mo–3mo) this is cheap; at full
   6.5-year scale (≈2M rows × ≈5,600 trades) it dominates the runtime —
   this is what caused the full campaign to run for hours instead of
   ~30 minutes even after every other fix above. m1 is sorted ascending
   by timestamp by construction, so `Series.searchsorted` finds the
   identical cutoff position in O(log n); replaced every such filter
   with a positional `.iloc[]` slice from the searchsorted position.

**A caught-and-reverted regression** (documented for the record): an
early version of fix 3 converted the entire remaining trade window to
`.to_dict("records")` *eagerly* before iterating. This looked correct
and passed golden-snapshot/tests, but made runtime *worse* at scale —
most trades close within a handful of candles, but `window` itself spans
from entry to the *end of the dataset*, so eager conversion did far more
work than the lazy `.iloc[i]` it replaced. Caught by benchmarking, not
just testing — a reminder that behavior-preservation tests catch
correctness regressions, not performance regressions.

**Combined effect of 2a–2e** (clean, no-profiler-overhead numbers):

| size | baseline | after 2a–2e | speedup |
| --- | --- | --- | --- |
| 1mo | 159.1s | 14.1s | **11.3x** |
| 3mo | 542.8s | 42.6s | **12.8x** |

---

## 3. Behavior Preservation

`scripts/golden_snapshot.py` captures every signal, trade, and metric
from a full pipeline run as sorted, stably-serialized JSON. Every single
optimization in this task was verified against it — `compare` reports
either `IDENTICAL` or the first diverging record. Every optimization
listed above compared `IDENTICAL` against the pre-optimization baseline
snapshot, and the full test suite (175 tests: 138 core + 16 strategy +
21 research) passed after every change.

---

## 4. Scaling Benchmark

`scripts/benchmark_scaling.py` measures wall time, CPU time, peak RSS,
and candles/sec across 1mo/3mo/1yr/3yr/full (6.5yr) dataset slices, and
reports a `scaling_factor_vs_prev` (time-ratio ÷ candle-ratio between
consecutive sizes — 1.0 means perfectly linear).

Mid-optimization sweep (after 2a–2e, before the entry.py/stop_loss.py
full-scan fixes) showed scaling degrading at large scale:

| size | candles | wall time | candles/sec | scaling factor |
| --- | --- | --- | --- | --- |
| 1mo | 31,653 | 12.9s | 2445.9 | — |
| 3mo | 93,383 | 39.8s | 2343.6 | 1.04 |
| 1yr | 372,275 | 256.4s | 1452.1 | 1.61 |
| 3yr | 745,080 | 619.4s | 1202.9 | 1.21 |
| full | 1,992,216 | 2700.2s (45.0min) | 737.8 | 1.63 |

This was the evidence that motivated the final round of fixes (2e's
`_true_range_atr`/`entry.py`/`engine.py` full-dataframe filters) — those
functions are called once per trade/signal, so their O(n) cost only
becomes dominant once the dataset (and trade count) is large enough,
which is exactly why it wasn't visible in the 1mo/3mo profiling used
for fixes 2a–2d. After the fix, 3-year re-profiling showed a healthy
605.7s (vs. 619.4s before — comparable, confirming no regression) with
2,325 trades over 745,080 candles, and the real full 6.5-year campaign
(Section 6) completed in under 33 minutes end-to-end, including exports
and analysis stages beyond just the three core pipeline stages measured
here.

**Known limitation**: peak RSS grows substantially with dataset size
(259MB at 3mo → 1.2GB at full), and even after all fixes, per-trade/
per-candle lookup functions like `fresh_order_block_asof` still show
measurable cumulative cost at multi-year scale (real work, not an
algorithmic bug — every open Order Block genuinely needs to be
considered). Scaling is now dominated by legitimate O(n log n) /
O(candles × small-constant) work rather than O(n²) bugs, but is not
perfectly linear. For future 10–15yr multi-symbol research, the next
lever would be reducing peak memory (e.g., processing in chronological
chunks) rather than further algorithmic fixes.

---

## 5. Progress Instrumentation

**File**: `src/utils/perf.py` — `ProgressReporter`

Long-running commands (a multi-year campaign can run for tens of
minutes) now print live progress: stage name, % complete, candles
processed, elapsed time, ETA (extrapolated from observed average
throughput), peak RSS, and current throughput. Printing is throttled to
at most once every 2 seconds (always printing on stage transitions and
100% completion) so high-frequency callers — e.g. one checkpoint per
trade during a multi-thousand-trade backtest — don't flood the console.

Design constraint: **no hooks were added inside trading-logic loops**.
`run_strategies` (`src/strategies/runner.py`) and `run_backtest`
(`src/backtest/engine.py`) both gained an optional `progress_cb`
parameter, defaulting to `None` (no-op, zero effect on any existing
caller) — called only with counters (strategy ID; trades-processed /
total), never with signal or trade data, so it's a pure observability
side-channel that cannot influence what gets computed.
`run_validation_campaign` wires these into a `ProgressReporter` when
`show_progress=True` (the default), with stage weights (load/strategies/
backtest/market_conditions) approximating each stage's relative share of
total wall time — an ETA aid only, never affecting correctness.

Example output (from a live campaign run):
```
[validation_campaign] stage=EURUSD:strategies:S3  41.0% (816,805/1,992,216 candles) elapsed=24m49s eta=35m44s peak_rss=1272.4MB throughput=548.2 candles/s
[validation_campaign] stage=EURUSD:backtest  88.6% (1,764,867/1,992,216 candles) elapsed=32m33s eta=4m11s peak_rss=1483.4MB throughput=903.3 candles/s
```

---

## 6. Full 6.5-Year Validation Campaign — Final Result

Re-run via `scripts/run_validation_campaign.py --raw-dir data/raw
--provider histdata --processed-dir data/processed/historical --out-dir
reports/validation_campaign_task74`, after every optimization in this
task:

| metric | Task 7.3 baseline | Task 7.4 result |
| --- | --- | --- |
| status | **killed after ~141 min, never finished** | **completed successfully** |
| total runtime | N/A (incomplete) | **32m51s (1,971s)** |
| candles processed | 1,992,216 | 1,992,216 |
| trades generated | N/A (incomplete) | 5,632 |
| peak RSS | not measured | 1,483.4 MB |
| improvement factor | — | **≥4.3x** (and now actually completes) |

All expected outputs generated in `reports/validation_campaign_task74/`:
`strategy_rankings.csv`, `portfolio_rankings.csv`,
`market_condition_analysis.parquet`, `confidence_validation.parquet`,
`trade_history.parquet`, `failure_analysis.parquet`,
`portfolio_correlations.parquet`, `dataset_manifest.json`,
`validation_summary.md`, `research_dashboard.html`,
`validation_report.pdf`.

---

## 7. Acceptance Criteria — Status

| criterion | status |
| --- | --- |
| Identical outputs to pre-optimization baseline | ✅ verified via golden snapshot at every step |
| Passes all existing tests | ✅ 175/175 throughout |
| 6.5-year campaign completes substantially faster | ✅ 32m51s vs. an incomplete 141+ min run |
| Live progress info for long-running commands | ✅ `ProgressReporter` |
| Predictable scaling for future multi-year/multi-symbol research | ✅ O(n²)-class bugs eliminated; residual scaling is memory-bound, not algorithmic (see §4 limitation) |
