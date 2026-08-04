# Research & Optimisation Laboratory Specification (v1.0, Task 5)

This document specifies `src/research/` -- the layer that turns the
platform from "did the strategy make money" into "why, under what
conditions, and which filters actually help." **This is still not live
trading, and uses no machine learning** -- every analysis here is
deterministic grouping/comparison arithmetic over Task 3/4 outputs.

## Architecture

```
src/research/
  experiment.py           -- Experiment dataclass + run_experiment() (reproducible unit of work)
  parameter_sweep.py      -- grid_sweep (brute-force) + coordinate_sweep (greedy, non-brute-force)
  sensitivity.py           -- single-parameter response curves (built on parameter_sweep)
  market_conditions.py     -- trending/ranging, high/low vol, bull/bear, gap days, news-day placeholder
  analysis_utils.py        -- shared grouped-metrics helper (num_trades/win_rate/profit_factor/expectancy/avg_r/drawdown)
  strategy_analysis.py     -- full per-strategy metric suite incl. Calmar Ratio (the one metric Task 4 didn't already compute)
  symbol_analysis.py       -- performance by symbol + ranking
  session_analysis.py      -- performance by session + session-overlap detection
  confidence_analysis.py   -- performance by confidence bucket + Spearman correlation check
  filter_analysis.py       -- With/Without comparison for any named boolean predicate over a Trade
  portfolio_research.py    -- every strategy combination (single/pair/triple/all), diversification benefit
  walkforward_research.py  -- rolling train/test windows, evaluated separately
  reporting.py              -- the 7 required parquet datasets + independent per-experiment CSV/JSON/Markdown/HTML export
  visualizations.py         -- one function per required chart type + a combined dashboard
```

Nothing here modifies `src/strategies/`, `src/backtest/`, `src/features/`,
or `src/structure/` -- this layer only calls into them.

## Research workflow

```
1. Build ONE MarketContext for a dataset (symbol + M1 data)
2. run_experiment(...) -> Experiment (signals -> trades -> metrics, reproducible)
3. Analyze:
     market_conditions.classify_market_conditions + label_trades_with_conditions
     symbol_analysis / session_analysis / confidence_analysis / filter_analysis
     portfolio_research.analyze_portfolio_combinations
     walkforward_research (rolling windows)
     parameter_sweep / sensitivity (vary configs, re-run experiments, reusing the SAME context)
4. reporting.save_research_datasets(...) -> the 7 parquet outputs
5. visualizations.build_research_dashboard(...) -> HTML dashboard
```

## Experiment lifecycle

Every `Experiment` records: `research_id` (a deterministic SHA-256 hash
of experiment_name + dataset + configuration + parameter_set -- NOT of
the wall-clock `timestamp` field, so re-running the identical experiment
twice always produces the same id even though `timestamp` itself
naturally differs per run), `experiment_name`, `timestamp`, `strategy`,
`configuration` (every config object used, serialized), `dataset` (a
`symbol:start/end` string), `parameter_set` (the specific values under
test, empty for a baseline run), `results` (metrics + trade list), and
free-text `notes`.

**Reproducibility is structural**: `run_experiment` reads no randomness,
no wall-clock state, and no global mutable state -- given the same
inputs it always returns the same `results`
(`tests/test_research.py::test_experiment_reproducibility`).

### Performance: context reuse across experiments

`run_experiment(..., context=...)` accepts an already-built
`MarketContext`. Every sweep/portfolio/walk-forward function in this
module builds ONE context per dataset and reuses it across every
experiment run on that dataset -- swings/structure/Order Blocks/FVGs/
liquidity are computed once and cached on the context object (see
`src/strategies/context.py`), not re-derived from scratch per experiment.
This is the difference between a 6-value parameter sweep taking ~6x one
experiment's cost vs. ~6x the FULL pipeline cost (context construction is
typically the majority of a single experiment's runtime).

## Parameter testing (not brute force only)

Every sweepable parameter is identified by `(target_key, field)`:
`target_key` in `{"S1".."S5", "entry_config", "stop_config", "tp_config",
"execution_config", "risk_config", "management_config"}`, `field` a
field name on that dataclass. This covers every parameter named in the
brief: strategy-level filters (min gap size, confidence threshold, OB
freshness, FVG/engulfing/liquidity requirements, CHoCH/BOS timeframe) and
execution-level choices (target style, stop style, risk:reward, max
trade duration, breakeven/trailing rules) alike, through one mechanism.

- **`grid_sweep`**: full cartesian product of every parameter's candidate
  values. Exact, but O(product of candidate counts) -- explosive for more
  than 2-3 parameters.
- **`coordinate_sweep`**: a greedy, one-parameter-at-a-time search. For
  each parameter (in the given order), holds every other parameter at its
  current best-known value and searches only that parameter's candidates,
  keeping the best value (by a chosen metric, default expectancy) before
  moving to the next parameter. O(sum of candidate counts) instead of
  O(product) -- for 6 parameters x 5 values, 30 experiments instead of
  15,625. This is the "intelligent... not brute force only" sweep the
  brief asks for: a simple, deterministic, non-ML greedy search, not full
  joint optimisation (that remains a future task).

`sensitivity.parameter_response_curve` is the single-parameter special
case, used for exactly the confidence-threshold response-curve example in
the brief (50/55/.../80 -> expectancy/profit factor/trade count).

## Strategy analysis

Every metric named in the brief (expectancy, profit factor, win rate,
average winner/loser, drawdown, trade duration, R-multiple distribution,
MAE, MFE, recovery factor, Sharpe, Sortino) is already computed by
`src.backtest.metrics.compute_performance_metrics` (Task 4) -- this
module adds **Calmar ratio** (net profit / |max drawdown|, annualization-
free since it's computed the same way as recovery factor here; see Known
Limitations) via `src.backtest.portfolio.compare_strategies`, which this
layer calls directly to build the required strategy-comparison table.

## Market condition analysis

`classify_market_conditions(m1)` labels every M1 candle causally (each
row's classification uses only a TRAILING rolling window ending at that
row -- never future candles):

- **Trending vs. ranging**: directional efficiency = |net move| / sum(|candle-to-candle moves|)
  over a trailing window; high efficiency -> trending, low -> ranging (choppy).
- **High vs. low volatility**: current rolling ATR vs. its own trailing
  expanding median.
- **Bull vs. bear**: sign of the trailing return over a longer window
  (default 1 day of M1 candles).
- **Gap days**: reuses `src.features.reference_levels.compute_weekend_gaps`
  (Task 2) -- no duplicated gap-detection logic.
- **News days**: **placeholder, always False** -- no economic-calendar
  feed is integrated in this task (see Known Limitations).

`label_trades_with_conditions` attaches the condition in effect at each
trade's `entry_timestamp` to `trade.metadata` via an as-of lookup (never
a future row).

## Symbol / Session / Confidence / Filter analysis

All four share `analysis_utils.group_metrics` (num_trades, win_rate,
profit_factor, expectancy, average R, net profit, max drawdown %) grouped
by a key function -- symbol, session, confidence bucket (10-point bins),
or an arbitrary filter predicate.

- **Session overlaps**: `session_analysis.analyze_session_overlaps` groups
  trades by the FULL SET of sessions active at entry (e.g. "london" vs.
  "london + new_york"), reusing `MarketContext.session_active_asof`.
- **Confidence correlation**: `confidence_analysis.confidence_profitability_correlation`
  computes the Spearman rank correlation between confidence score and
  both realized PnL and R-multiple -- this is the actual test of whether
  the confidence model has predictive value, not just a bucketed table.
- **Filter effectiveness**: `filter_analysis.compare_filters` takes
  `{name: predicate}` and reports a With/Without expectancy delta and a
  verdict (`improves`/`reduces`/`neutral`) per filter. Built-in
  predicates: `has_fvg`, `has_engulfing`, `has_liquidity_sweep` (all
  derived from a trade's `reason_codes`), `gap_above_pips(n)` (from S1
  trades' gap metadata), and `fresh_ob_predicate(context)` (looks up each
  trade's referenced Order Block by id in the full-history OB dataset --
  see the "OB freshness is post-hoc, not look-ahead" note below).

## Portfolio research

`portfolio_research.generate_combinations` builds every single strategy,
every pair, every triple, and the full combined set (deduplicated).
`analyze_portfolio_combinations` reports return, drawdown, volatility
(std of daily realized PnL), and **diversification benefit** = `1 -
portfolio_volatility / mean(individual_strategy_volatilities)` -- positive
means combining actually reduced volatility relative to the strategies'
average, not just added more trades. `portfolio_correlation_summary`
reuses Task 4's `strategy_correlation` directly.

## Walk-forward research

`walkforward_research.generate_rolling_windows` produces a sequence of
test periods stepping forward through the dataset (with informational
train-period boundaries for a future optimisation task to use).
Trades are **never re-simulated per window** -- `run_backtest` runs once
over the full dataset (preserving the lookback context every trade near
a window boundary needs), and each window simply filters the resulting
trades to those entered in its test range. `summarize_stability` reports
how many windows had positive expectancy and the expectancy mean/std
across windows -- a strategy whose aggregate backtest looks good but
whose per-window expectancy flips sign constantly is a red flag this
surfaces directly.

## Sensitivity analysis

`sensitivity.parameter_response_curve` = `parameter_sweep.grid_sweep`
with exactly one parameter, sorted by the parameter's value --
`visualizations.sensitivity_curve_chart` plots expectancy/profit factor/
trade count side by side against that value, matching the brief's
worked example precisely. `detect_diminishing_returns` gives a cheap,
deterministic (non-statistical) signal for whether the curve has
flattened at its current range's edge.

## Reporting & exports

`reporting.save_research_datasets` writes all seven required datasets:
`research_summary.parquet` (one row per experiment, flat summary),
`experiment_results.parquet` (full configuration/parameter_set/results,
JSON-encoded), `parameter_analysis.parquet`, `portfolio_analysis.parquet`,
`confidence_analysis.parquet`, `filter_analysis.parquet`,
`walkforward_results.parquet`. `export_experiment` writes ONE experiment
independently in CSV, Parquet, JSON, Markdown, and HTML.

## Visualisations

One function per required chart type in `visualizations.py`: equity
curves, drawdown comparison, parameter heatmap, sensitivity curves,
confidence distribution, strategy comparison, portfolio comparison,
trade distribution, session performance, symbol performance, correlation
matrix, monthly returns, expectancy distribution -- plus
`build_research_dashboard` combining a curated subset into one HTML.

## Known limitations

- **News days are a placeholder** (always `False`) -- no economic
  calendar feed is wired up; adding one (e.g. a CSV of scheduled
  high-impact release timestamps) is explicit future work, not attempted
  here since it requires an external data source out of scope for this task.
- **Calmar ratio** is reported as net profit / |max drawdown| without a
  separate annualization step distinct from Recovery Factor (Task 4)
  because this research layer's datasets are short (days-to-weeks, not
  years) -- a true annualized Calmar needs a multi-year backtest to be
  meaningful, which is itself a data-availability limitation, not a
  computation bug.
- **`coordinate_sweep` is greedy, not globally optimal**: parameter
  interaction effects (where the best value of parameter B depends on
  parameter A's value) can be missed if A is swept before B settles into
  its true best combined value. This is the standard trade-off for
  avoiding a full combinatorial search; a future task could add a second
  refinement pass (re-sweep earlier parameters once later ones have been
  chosen) if interaction effects turn out to matter.
- **OB-freshness filter is post-hoc-safe by design, not by accident**:
  `fresh_ob_predicate` looks up each trade's Order Block using the FULL
  batch history's `freshness_status` -- this is safe specifically because
  filter analysis explains ALREADY-CLOSED historical trades after the
  fact; it must never be reused to gate a live/streaming decision (that
  would need the point-in-time reconstruction already used in
  `src.strategies.context.fresh_order_block_asof`).
- **Trending/ranging and volatility classification are simple, causal
  heuristics** (directional efficiency, ATR-vs-median), not a validated
  regime-detection model -- adequate for grouping trades into buckets
  for research, not a trading signal itself.
- **Diversification benefit** uses daily PnL standard deviation as the
  volatility proxy; it does not account for intraday drawdown timing
  overlap between strategies (two strategies could have zero daily-PnL
  correlation yet still draw down simultaneously intraday).

## Future extensions

- Wire an actual economic-calendar feed for real news-day classification.
- A genuine walk-forward *optimizer* (fit parameters per training window,
  validate out-of-sample) on top of the window-generation machinery
  already built here.
- A second refinement pass for `coordinate_sweep` to catch parameter
  interaction effects the first greedy pass misses.
- Multi-symbol portfolio research once real multi-symbol datasets are
  available (this task's examples --EURUSD, GBPUSD, USDJPY, XAUUSD,
  NAS100, US30, BTCUSD-- are named as illustrations; the module works on
  however many symbols are actually supplied).
