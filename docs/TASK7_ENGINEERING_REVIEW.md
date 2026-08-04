# Task 7 / 7.1 Engineering Review — Handoff Audit

Scope: independent review of the Historical Data Pipeline
(`src/data/historical_pipeline.py`), the Dukascopy downloader
(`src/data/providers/dukascopy.py`, `scripts/download_history.py`), and
the Real Market Validation Campaign
(`src/research/validation_campaign.py`, `scripts/run_validation_campaign.py`)
built in a prior session. All findings below were confirmed by reading
the code AND by actually executing it — nothing here is speculative.

---

## 1. Engineering Review Report

**Architecture**: clean and consistent with the rest of the platform.
`historical_pipeline.py` extends the Task 1 loader pattern (adapter
protocol, `ValidationReport`, quality scoring) rather than replacing it.
`dukascopy.py` sits below it as a provider, feeding normalized M1 data
back through `build_standard_dataset`. `validation_campaign.py` composes
Task 3/4/5 primitives (`MarketContext`, `run_strategies`, `run_backtest`,
`classify_market_conditions`, `analyze_portfolio_combinations`) directly
rather than reimplementing any of them — this is the correct integration
pattern and the main reason the campaign inherits Task 3/4/5's existing
anti-look-ahead guarantees for free.

**Coding standards**: consistent with the existing codebase (module
docstrings, `from __future__ import annotations`, dataclass-based
records, `pathlib.Path` throughout). No stylistic drift from Tasks 1–6.

**Duplicated functionality found and fixed**: `validation_campaign.py`
had its own private `_write_parquet` that reimplemented the exact
JSON-encode-then-write logic already in
`src/features/storage.py::save_feature_dataset`. Fixed by extending
`save_feature_dataset` with an optional `index` parameter (needed for the
correlation-matrix export) and having `validation_campaign.py` delegate
to it instead of maintaining a parallel copy.

**Look-ahead bias**: none found in the new code. The campaign correctly
delegates all decision-relevant computation (signal generation,
backtesting, market-condition classification) to already-audited Task
3/4/5 functions and only adds read-only aggregation/reporting on top.

**Downloader integration with the validation campaign**: correct. The
downloader's `build_m1=True` path runs the SAME `build_standard_dataset`
the campaign uses for CSV/Parquet inputs, so a symbol acquired via
Dukascopy and one supplied manually go through identical validation.

---

## 2. Test Results

Full suite (excluding two known-slow full-pipeline files, run
separately): **121 passed, 0 failed** (69s).
Slow full-pipeline suites (`test_strategies.py`, `test_research.py`):
**37 passed, 0 failed** (334s — these drive the complete Task 3→4
pipeline over multi-day synthetic data; this is expected, not a defect).
**Project total: 158/158 passing** after fixes below.

One test failed on the first clean run and has been fixed (see Bug
Report #1) — a bug in the test's own data construction, not the
production code.

Linting: `ruff check .` → 96 findings project-wide (43 are `E402` in
`scripts/*.py`, an established, unavoidable pattern for standalone
scripts that must `sys.path.insert` before importing project modules;
the rest are routine unused-import/unused-variable notices spread fairly
evenly across old and new code — Task 7/7.1 does not measurably worsen
the project's pre-existing lint debt, which has never been gated by a
ruff config). Not fixed project-wide — that is a repo-wide style
decision out of scope for this review, not a Task 7 regression.

Type-checking: `mypy` on the three new modules directly reports **zero
errors in those files**; the 19 errors it surfaces are all in
transitively-imported Tasks 1–6 modules (`backtest/metrics.py`,
`backtest/portfolio.py`, `features/liquidity.py`, `features/displacement.py`,
`strategies/common.py`, `research/portfolio_research.py`) that predate
this review and have never been type-checked. Pre-existing debt, not a
Task 7 issue.

---

## 3. Bug Report

### Bug #1 (Critical, confirmed, fixed) — Silent data loss under concurrent downloads
`DukascopyDownloader.download_range` fans out one worker thread per
`(symbol, day)` job. Every day for the same symbol appends its M1
candles to the **same** per-symbol `campaign_m1_path` CSV via
`append_csv_dedup` (read-modify-write: load existing CSV, concat, dedup,
write back) — called from inside `download_day`, i.e. from worker
threads, unsynchronized.

**Confirmed empirically**: downloading 30 days for one symbol with the
config default `workers=4` (tested at `workers=8` for a clearer signal)
resulted in all 30 days individually reporting `status="complete"`, but
only **22–24 of 30 unique dates survived** in the shared CSV — 6-8 days
of real, successfully-downloaded data silently vanished because two
threads' read-modify-write cycles overlapped and the last writer
discarded the other's day. With `workers=1`, all 30/30 survive correctly.
This is exactly the "10-15 years of EURUSD M1" workload the platform is
meant to support, and it was silently corrupting output by design.

**Fix**: added a per-output-path `threading.Lock` (`_lock_for_path` in
`dukascopy.py`) so concurrent days for the same symbol serialize their
writes to the shared file while different symbols keep running in
parallel. Re-tested 3x at `workers=8`: 30/30 days survive every time.
Added `tests/test_dukascopy_downloader.py::test_concurrent_days_do_not_lose_data_in_shared_campaign_csv`
as a permanent regression guard.

### Bug #2 (Critical, confirmed, fixed) — Campaign crashes on any dataset with a weekend gap
`validation_campaign.py::_market_condition_frames`'s `"gap_day"`
dimension returned a Python `bool`, while every other dimension
(`trend_state`, `volatility_state`, `directional_bias`, `session`)
returns a string. Once concatenated into one `condition` column, PyArrow
refuses to write the resulting mixed-type (`bool`/`str`) column to
Parquet.

**Confirmed empirically**: running the full campaign against a realistic
10-day synthetic dataset (which spans a real weekend, unlike the
existing 5-row test fixture) crashed with
`ArrowTypeError: Expected bytes, got a 'bool' object` while writing
`market_condition_analysis.parquet` — meaning the campaign as delivered
would fail on **any real multi-week FX dataset**, since weekend gaps are
a routine weekly occurrence, not an edge case. The existing test suite
never caught this because its only "real dataset" fixture was 5 rows
with no weekend in it.

**Fix**: changed the gap-day predicate to return `"gap_day"`/`"normal_day"`
strings. Re-ran the full campaign end-to-end against the 10-day dataset:
all 11 output files generate correctly, including `market_condition_analysis.parquet`.
Added `tests/test_validation_campaign.py::test_validation_campaign_survives_dataset_with_a_weekend_gap_day`.

### Bug #3 (Medium, confirmed, fixed) — `market_condition_analysis` had no `symbol` column
`_market_condition_frames` grouped trades by condition only, with no
symbol tag. After concatenating results across multiple symbols (the
campaign's actual multi-symbol use case), two otherwise-identical rows
(e.g. "trending" from EURUSD and "trending" from GBPUSD) were
indistinguishable in the output — a real usability/correctness gap for
the exact multi-symbol comparison the campaign exists to produce.
**Fix**: added a `symbol` column, threaded through from the per-dataset
loop in `run_validation_campaign`. Regression test added.

### Bug #4 (Medium, confirmed, fixed) — Resume never skips confirmed no-data days
`catalogue_has_success` (used to decide whether `--resume` should
re-attempt a day) only ever skipped days with `validation_status=="valid"`.
A day where every one of the 24 hourly files legitimately 404's (i.e. a
weekend — Dukascopy simply has no data) is marked `status="missing"`,
which was **never** treated as resolved. Consequence: every `--resume`
run over a multi-year range re-issues all 24 hourly requests for every
weekend day, every single time, forever. Over 10-15 years that is
several thousand guaranteed-404 requests repeated on each resume — pure
waste, and (see Bug #5) directly compounds the rate-limiting risk.
**Fix**: `catalogue_has_success` now also treats `status=="missing"` as
resolved-and-skippable, while `status=="failed"` (a real error that
survived retries) is deliberately still retried. Regression test added.

### Bug #5 (Test bug in prior work, fixed) — Self-defeating gap-detection test
`tests/test_historical_pipeline.py::test_duplicate_and_gap_detection`
(pre-existing, from the same prior session) constructed its "gap"
scenario by overwriting a duplicate row's timestamp onto the first row
and dropping a third row — which collapses the dataset to a single
unique timestamp, making a gap structurally impossible to observe (there
is no timestamp range left to have a hole in). This failed on a clean
run. Fixed by constructing a duplicate via an appended row (not an
overwrite) so a real timestamp range with an actual gap survives dedup.

### Findings documented but not changed (design gaps, not crashes)
- **No HTTP 429 / backoff handling**: live-network testing during this
  review hit Dukascopy's real endpoint and immediately received
  `HTTP 429 Too Many Requests`. `fetch_with_retries` retries on any
  `HTTPError` but has no `Retry-After` awareness and
  `request_pause_seconds` defaults to `0.0` — under real rate-limiting,
  retries fire back-to-back with no backoff, which is likely to make the
  limiting worse rather than recover from it. Not fixed (would require a
  larger redesign of the retry policy); flagged as the top production
  blocker in the Readiness Assessment below.
- **`PRICE_SCALE` only covers USDJPY/XAUUSD** (both scaled x1000, vs. the
  x100000 FX default). `DEFAULT_SYMBOLS` in the campaign doesn't include
  NAS100/US30/BTCUSD, so this isn't reachable today, but if the
  downloader is later pointed at those instruments without a scale entry,
  prices would be **silently wrong** (no error, just nonsense values) --
  a latent landmine worth a one-line comment/guard in a future task.
- **`timezone_inconsistencies` is effectively always `0`** in normal use:
  it only flags an issue when `source_tz` is falsy AND the raw data has
  no embedded tz info; since every call site in this codebase always
  passes `source_tz="UTC"` explicitly, the field never actually compares
  a caller-provided timezone against a conflicting embedded one. Not a
  crash, just a validation check that is currently closer to a
  placeholder than a real cross-check. Left unchanged (fixing it well
  requires deciding what a "real" mismatch should look like, which is a
  product decision, not a bug fix).
- **No global rate limiter across the worker pool**: `request_pause_seconds`
  only pauses between retries of the *same* request; concurrent workers
  otherwise hit the endpoint as fast as they can. Combined with the 429
  finding above, this is a real risk for a genuine 10-15-year, multi-symbol
  download run.

---

## 4. Performance Review

- Downloader throughput is dominated by network latency per hourly file,
  not local compute; the confirmed concurrency bug (now fixed) was the
  only correctness-affecting performance issue found.
- `append_csv_dedup` re-reads and rewrites the *entire* per-symbol CSV on
  every single day processed (O(n) rewrite × n days = O(n²) total I/O
  over a multi-year range). For 10-15 years (~3,650-5,475 trading days)
  this is a real, measurable inefficiency, though not a correctness bug —
  flagged as a priority optimization target below (append-only or
  periodic-compaction storage would remove it).
- `historical_pipeline.build_standard_dataset`'s gap-detection
  (`_detect_gaps`) builds a full `pd.date_range` at the expected interval
  between the dataset's min/max timestamp and diffs it against actual
  timestamps — for a 10-15 year M1 dataset that is 5-8 million expected
  timestamps materialized in memory just to find gaps. Works, but is the
  same class of cost Task 1's loader already had at this scale; nothing
  new introduced here, just inherited.
- No memory blow-ups observed in any test run at the scales exercised
  (10-day and 30-day synthetic datasets). Multi-year scale was not
  executed live (see Readiness Assessment) so this is a projection based
  on the code path, not a measurement.

---

## 5. Architecture Review

No redesign performed or needed. The layering (`historical_pipeline` as
the provider-agnostic normalization layer; `dukascopy` as one provider
under it; `validation_campaign` as a pure consumer of Tasks 3/4/5) is the
right shape and matches how the rest of the platform is organized. The
one structural fix made (`save_feature_dataset` gaining an `index`
parameter) was an additive, backward-compatible change, not a redesign.

---

## 6. Production Readiness Assessment

**Is the downloader production ready?** Not yet, but close. The
concurrency data-loss bug (#1) and the resume inefficiency (#4) are now
fixed and regression-tested. The unresolved 429/backoff gap is the
remaining blocker for genuinely large (10-15 year, multi-symbol) runs
against the real Dukascopy endpoint without manual babysitting.

**Is the validation campaign complete?** Functionally yes for its stated
scope (it correctly refuses to run on synthetic-only data, and now
correctly completes on a realistic dataset including a weekend gap,
producing all 11 documented output files). Bug #2 would have made it
non-functional on literally any real dataset before this review; it is
fixed and tested now.

**Is the historical pipeline robust?** Yes for the validation checks it
implements (duplicates, OHLC integrity, gaps, weekend rows). The
`timezone_inconsistencies` check is weaker than its name implies (see
Bug Report) but this does not block correct operation given the
project's consistent "always pass source_tz=UTC explicitly" convention.

**Can the platform reliably process 10-15 years of EURUSD M1 data?**
Conditionally yes, now that Bug #1 is fixed — before this review, the
answer was no (silent, non-obvious data loss at exactly that scale).
Remaining risks at that scale: the 429/backoff gap could stall or
partially fail a real multi-year download unattended, and the O(n²)
CSV-rewrite cost in `append_csv_dedup` will become noticeably slow
(untested at that scale) well before 10-15 years of daily files
accumulate.

**Remaining blockers before large-scale research begins:**
1. Real-network 429/backoff handling in the downloader (highest
   priority — confirmed reachable on the very first live request made
   during this review).
2. Replace `append_csv_dedup`'s full-file-rewrite-per-day pattern with
   an append-only or batched-compaction strategy before running a
   multi-year download.
3. A live, supervised trial run (even 1-3 months for one symbol) against
   the real Dukascopy endpoint has not yet been performed successfully
   end-to-end in this environment (rate-limited on the first request) —
   this should happen before committing to a full 10-15 year run.

---

## 7. Recommendation for the Next Development Milestone

**Priority ranking** (highest first):
1. Downloader resilience: 429/backoff handling + rate-limit-aware
   pacing across the whole worker pool (directly blocks any real
   large-scale download).
2. Storage efficiency: replace `append_csv_dedup`'s O(n²) per-day
   rewrite with an append-only/batched-compaction pattern (blocks
   comfortable multi-year runs).
3. A real, supervised end-to-end download of 1-3 months of EURUSD to
   validate the fixes in this review against the live Dukascopy service
   (not just injected-fetcher unit tests).
4. Only after 1-3 succeed: run the actual Task 7 validation campaign on
   real data and produce the first genuine research findings.

This next milestone should be scoped narrowly around **making the
downloader trustworthy at scale**, not new research features — the
platform's research/backtesting layers (Tasks 1-5) are already validated
and stable; the acquisition layer is the only piece that hasn't yet
proven itself against the real world.

### Complete implementation prompt for the next milestone

```
TASK 7.2 — FOREX SMC QUANT SYSTEM
Downloader Resilience & Real-Data Validation Run

Context: Task 7.1's Dukascopy downloader has two confirmed, now-fixed
bugs from engineering review (concurrent-write data loss; resume not
skipping confirmed no-data days) plus two known, unresolved gaps:
(a) no HTTP 429 / backoff handling -- confirmed reachable on the very
first live request made against the real Dukascopy endpoint during
review, and (b) an O(n^2) per-day full-file-rewrite in the shared
per-symbol CSV writer (append_csv_dedup), which will become a real
bottleneck before 10-15 years of daily files accumulate.

Do NOT redesign the Market Structure Engine, SMC Feature Engine,
Strategy Engine, Backtesting Engine, or Research Laboratory. Do NOT add
machine learning, MetaTrader integration, broker APIs, or live trading.

OBJECTIVE 1 -- Rate-limit-aware retries
Add HTTP 429 detection to fetch_with_retries/download_hour in
src/data/providers/dukascopy.py. On a 429, honor a Retry-After header if
present; otherwise back off with exponential delay (configurable base
and cap in DukascopyDownloadConfig). Add a global, cross-thread rate
limiter (e.g. a token bucket or simple semaphore-with-delay) so the
whole worker pool -- not just retries of a single request -- respects a
configurable max-requests-per-second, since concurrent workers currently
have no shared throttle.

OBJECTIVE 2 -- Efficient shared-file writes
Replace append_csv_dedup's read-entire-file-concat-rewrite-entire-file
pattern (called once per day) with an approach that does not re-read/
re-write the whole accumulated file on every single day: options include
appending new rows directly and deferring deduplication/sorting to a
single compaction pass at the end of a download_range call, or switching
the per-symbol aggregate to Parquet with row-group appends. Preserve the
existing dedup-on-(timestamp,symbol) guarantee and the existing test
suite's behavioral expectations exactly -- this is a performance fix,
not a behavior change.

OBJECTIVE 3 -- Supervised real-data validation run
Using the fixed downloader, perform and document a real, live download
of 1-3 months of EURUSD M1 tick data from the actual Dukascopy service
(not an injected test fetcher). Record: wall-clock time, request count,
retry/backoff events actually triggered, final data quality score from
build_standard_dataset, and confirm zero missing days in the resulting
per-symbol CSV/Parquet via an independent row count check.

OBJECTIVE 4 -- First real validation campaign
Once Objective 3's data passes validation, run
scripts/run_validation_campaign.py against it and report the actual
(not synthetic) strategy rankings, portfolio rankings, and confidence
analysis it produces. Treat low trade counts as inconclusive per the
existing campaign's own stated interpretation guidance -- do not
over-interpret a 1-3 month sample as proof of edge.

TESTING: extend tests/test_dukascopy_downloader.py with deterministic
(injected-fetcher) tests for 429 handling and backoff timing (using a
fake clock, not real sleeps), and for the new storage strategy's
dedup/ordering guarantees under concurrent writes (reuse the existing
race-condition regression test's pattern). Do not add a test that
depends on real network access.

FINAL REPORT: files changed, test results, the real download's recorded
metrics (Objective 3), the real campaign's output (Objective 4), and an
updated production-readiness assessment -- specifically, whether the
platform is now ready for the full 10-15 year, multi-symbol download.
```
