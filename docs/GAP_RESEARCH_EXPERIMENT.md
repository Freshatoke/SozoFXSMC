# Gap Reversion Research Experiment (Task 12 Phase 14)

## What was actually run

`scripts/run_gap_robustness_experiment.py --start 2023-01-01 --end 2023-02-01 --n-configs 40 --seed 42 --results-dir reports/robustness/gap_eurusd_pilot`

- Experiment ID: `EXP_gap_reversion_eurusd_1d5f8e86` (registry: `reports/robustness/registry/EXP_gap_reversion_eurusd_1d5f8e86.json`)
- Dataset: `EURUSD_M1.parquet`, window `2023-01-01` to `2023-02-01` (31,523 M1 candles)
- Split: Train `2023-01-01 22:04` → `2023-01-19 03:38`; Validation → `2023-01-25 13:16`; Out-of-sample → `2023-01-31 23:59` (60/20/20, chronological, OOS locked until selection was frozen)
- 40 configurations sampled (seed 42) from the full parameter space in `scripts/run_gap_robustness_experiment.py::PARAM_SPACE` (gap size/direction, CHoCH/OB/FVG requirements, OB quality threshold, FVG size/retracement/fill rules, stop/target method, R:R, session/day-of-week/volatility filters, confidence threshold)
- Total runtime: 123.0s. Git commit at run time: `dd8ff317ed20`.

## Why 40 configurations, not 1,000-5,000

This is stated plainly rather than hidden: the task's brief targets 1,000-5,000 configurations. Three attempts at larger scale were made in this session:

1. 250 configs / 3-month window — process completed but its buffered stdout was lost when the session was interrupted; no report was generated.
2. 150 configs / 2-year window — after 45 minutes the search had not even reached its first 50-config progress checkpoint; killed as impractically slow for this session.
3. 200 configs / 3-month window (unbuffered) — after 112 minutes, only 50/200 configs had completed (~135s/config); the process was terminated when the session hit a usage limit and did not resume.

Root cause: `generate_gap_reversion_signals`' per-gap candle-window scan does not short-circuit efficiently for configurations with permissive filters (few/no CHoCH-OB-FVG requirements) — those configurations scan large spans of candles per gap before finding (or failing to find) a qualifying entry, and cost scales worse than linearly with the data window size. **This is a genuine, reportable performance limitation of the current research-layer implementation, not of the underlying reused `run_backtest`/`compute_performance_metrics` engine** (those remain fast; the bottleneck is this new module's own signal-generation loop). It is flagged here, not fixed, since fixing it was not in Task 12's scope and doing so hastily risked introducing a real bug into the newly-built code without adequate testing time.

The 40-configuration, 1-month run is the one experiment that completed cleanly, with a real generated report, real CSV/Parquet output, and a registered, reproducible experiment record. It is reported here as the actual Phase 14 result, not a larger number that was never actually completed.

## Results

| Metric | Value |
|---|---|
| Total configurations attempted | 40 |
| Failed/errored configurations | 0 |
| Configurations with >= 1 closed trade | 4 |
| Configurations with >= 10 trades (the framework's minimum for significance testing) | **0** |
| Raw p < 0.05 (uncorrected) | 0 |
| Bonferroni survivors | 0 |
| Benjamini-Hochberg survivors | 0 |
| Promoted to out-of-sample testing | 0 |

**Final verdict: NO ROBUST EDGE FOUND.** No configuration reached even the minimum trade count for the framework to test significance, let alone survive multiple-testing correction. Per this task's own explicit instruction, this is reported as a legitimate, successful research outcome — not a failure to be hidden or re-run until a better number appears.

## Why the result was this sparse — and what it actually shows

A 1-month window contains very few weekend gaps (a handful at most). Requiring a gap AND (optionally) CHoCH confirmation AND an Order Block AND/OR an FVG is a compound-rarity filter on top of an already-rare event — most of the 40 sampled configurations (36 of 40) produced zero trades in this window, and only 4 produced exactly one trade each. This is not a bug: it is the direct, honest consequence of (a) a short data window and (b) the gap-reversion hypothesis being a genuinely low-frequency setup, consistent with this platform's own prior findings (Tasks 8/9) that SMC-confirmed setups fire only a few times per week even for the strongest strategies (S3/S4) across ALL of their trigger conditions combined, let alone a single compound hypothesis like this one.

**This means the pilot experiment is UNDERPOWERED, not negative.** "NO ROBUST EDGE FOUND" here should be read as "no configuration in this short window produced enough trades to say anything statistically meaningful either way" — a data-volume limitation of this specific run, not evidence against the gap-reversion hypothesis itself. The framework correctly refused to manufacture a false signal from too little data (the `MIN_TRADES_FOR_SIGNIFICANCE = 10` gate exists exactly to prevent that).

## What a properly-powered run requires

Based on this session's timing evidence, a run with enough weekend gaps to produce statistically meaningful trade counts (a 12+ month window) needs either (a) a substantially longer single-session time budget than was available here, or (b) a performance fix to the signal-generation loop's window-scanning (bounding the per-gap scan more tightly, or vectorizing the filter checks) before attempting the originally-targeted 1,000-5,000-configuration scale. Recommended as the concrete next step, explicitly NOT undertaken in this task per its "do not modify production code without evidence of a bug, and even then STOP and report separately" instruction and its own time-boxing.

## Reproducing this experiment

```bash
python scripts/run_gap_robustness_experiment.py \
  --start 2023-01-01 --end 2023-02-01 --n-configs 40 --seed 42 \
  --results-dir reports/robustness/gap_eurusd_pilot
```

Given the same git commit, dataset file, seed, and date range, this reproduces byte-identical configuration sampling (verified by `tests/test_robustness.py::test_sample_configurations_reproducible_with_same_seed`) and — barring any change to the reused `run_backtest`/feature-detection code — identical results.
