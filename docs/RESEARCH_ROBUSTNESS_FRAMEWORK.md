# Research Robustness & Discovery Layer (Task 12)

## What this is and isn't

This is a **research-only** layer under `src/research/robustness/`. It does not modify, replace, or get called by `S1`-`S5`, `src/decision_engine/ios.py`, `src/research/itqs.py`, the Decision Engine, the Paper Broker, or the live scanner (`src/live/`, `scripts/telegram_scan_and_notify.py`). It exists to answer one question rigorously: **does an apparent edge, discovered by testing many parameterized configurations, survive increasingly hostile statistical tests — or is it a product of chance?**

Per `docs/VIDEO_METHODOLOGY_STATISTICAL_AUDIT.md`'s own math: testing 25,000 zero-edge configurations at an uncorrected p<0.05 bar would produce ~1,250 that "look profitable" by chance alone. This framework exists to make that number visible and corrected for, not to hide it.

## Architecture — what's reused vs. new

| Concern | Module | Status |
|---|---|---|
| Signal generation (gap-reversion hypothesis) | `src/research/robustness/gap_signals.py` | NEW — separate from production `s1_monday_gap.py` |
| Trade simulation | `src.backtest.engine.run_backtest` | REUSED, unmodified |
| Performance metrics | `src.backtest.metrics.compute_performance_metrics` | REUSED, unmodified |
| Train/validation/OOS split | `src.backtest.walkforward` | REUSED, unmodified |
| OOS access control | `src/research/robustness/data_split.py` | NEW (thin guard over the reused split) |
| Bounded configuration sampling | `src/research/robustness/search_engine.py` | NEW |
| Monte Carlo | `src/research/robustness/monte_carlo.py` | NEW |
| Multiple-testing correction | `src/research/robustness/multiple_testing.py` | NEW |
| Research Robustness Score | `src/research/robustness/robustness_score.py` | NEW |
| Parameter stability | `src/research/robustness/parameter_stability.py` | NEW |
| Cost/execution stress | `src/research/robustness/cost_stress.py` | NEW |
| Experiment registry | `src/research/robustness/registry.py` | NEW |
| Reporting | `src/research/robustness/report.py` | NEW |
| Walk-forward rolling windows | `src.research.walkforward_research` | REUSED, unmodified |

## The pipeline

```
ResearchDataset (train/validation/OOS, OOS locked)
        |
sample_configurations(param_space, n, seed)   -- bounded, seeded, deterministic
        |
run_search()  -- each config: generate_gap_reversion_signals -> run_backtest -> compute_performance_metrics
        |  (train+validation ONLY -- OOS remains locked)
        v
raw p-values (one_sample_t_test_pvalue)
        |
Bonferroni + Benjamini-Hochberg correction  -- survivors only
        |
ds.unlock_out_of_sample(reason=...)   -- explicit, logged, one-time
        |
re-run survivors on out_of_sample slice
        |
Monte Carlo (bootstrap + trade-order randomization) on OOS trade P&Ls
        |
parameter_stability (neighbor perturbation) on OOS-surviving configs
        |
cost_stress (baseline -> adverse_execution) on OOS-surviving configs
        |
compute_rrs()  -- Research Robustness Score, combining all of the above
        |
register_experiment() + write_results_csv() + render_experiment_report_markdown()
```

## Research Robustness Score (RRS)

```
RRS = 100 * (
    0.20 * oos_expectancy_component +
    0.15 * oos_consistency_component +
    0.15 * parameter_stability_component +
    0.15 * monte_carlo_survival_component +
    0.10 * drawdown_component +
    0.10 * significance_component +
    0.10 * cross_symbol_component +
    0.05 * sample_size_component
)
```

Every weight is a stated, declared judgment call — NOT fitted to data (fitting the score itself would reintroduce the exact overfitting risk this framework exists to control). A missing component scores 0, not "skipped" — an incomplete robustness check lowers RRS, per the governance rule that incomplete evidence must never be treated as favorable evidence. See `src/research/robustness/robustness_score.py` for the exact, documented formula and thresholds (>=70 STRONG_CANDIDATE, >=40 WEAK_CANDIDATE, else NOT_ROBUST — triage labels, not profitability claims).

**RRS is never read by, written to, or compared against IOS or ITQS.** It has no path into the live decision engine.

## Governance (Phase 12, enforced in code, not just policy)

- `search_engine.MAX_CONFIGURATIONS = 5000` — `run_search()` raises unless explicitly overridden with `allow_large_search=True`.
- `ResearchDataset.out_of_sample` raises `OutOfSampleLockedError` until `unlock_out_of_sample(reason=...)` is called explicitly, with a non-empty reason. This makes "the search engine never selects using OOS information" a structural guarantee, not a discipline the researcher has to remember.
- `ConfigResult.error` is populated (never a silent drop) when a configuration fails; `write_results_csv` writes every row, including failures.
- Reports render the full search universe count and failure count, never only the winner.

## Statistical honesty (see also `docs/VIDEO_METHODOLOGY_STATISTICAL_AUDIT.md`)

Every statistical test implemented here documents its own assumptions and limitations in its module docstring — Bonferroni is conservative because configurations are correlated, not independent; the Deflated Sharpe Ratio implementation is a documented simplification of Bailey et al.'s estimator, not the full closed-form version; Monte Carlo results never claim to predict future profitability. A configuration passing every gate in this framework is a **research candidate**, not a validated trading edge — forward/paper validation (Task 11's live infrastructure) remains the only thing that can actually validate live performance.

## Discovered production characteristic (not fixed here — see completion report)

Building the no-look-ahead test for `gap_signals.py` surfaced a pre-existing characteristic of `MarketContext.order_blocks()`/`fresh_order_block_asof()` (used identically by every S1-S5 strategy): Order Block detection is computed once over the full dataframe the context holds, without an `as_of_index` restriction (unlike `detect_order_blocks()` itself, which supports one). An OB whose `creation_timestamp` sits exactly at a given cutoff can be present when computed over a longer dataset and absent when computed over one truncated at that same cutoff. Per this task's explicit instruction, this was NOT fixed as part of Task 12 — flagged for separate follow-up.
