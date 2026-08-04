# forex-smc-quant

Research-first Forex algorithmic trading foundation based on Smart Money
Concepts (SMC). **This is not a live trading bot.** Task 1 built the
common data layer and a look-ahead-free market structure (BOS/CHoCH)
engine. Task 2 built the reusable SMC Feature Engine (Order Blocks, FVGs,
Displacement, Liquidity, Sessions, Reference Levels, Engulfing,
Confluence) on top of it. Task 2.5 replaced request-time confluence
recomputation with an incremental, event-driven engine (`src/engine/`) so
the whole system scales to millions of candles instead of recomputing
history on every snapshot. Task 3 built five independent research
strategies (`src/strategies/`) that consume all of the above — they
detect, validate, and record explainable signals; they do not execute
trades. Task 4 built a realistic Backtesting & Execution Simulator
(`src/backtest/`) that turns those signals into complete trade histories
and institutional-grade performance analytics. Task 5 built a Research &
Optimisation Laboratory (`src/research/`) that answers *why* a strategy
performs the way it does — parameter sweeps, market-condition/symbol/
session/confidence/filter analysis, portfolio combinations, rolling
walk-forward windows, and sensitivity curves. **No module in this
project trades live, connects to a broker, or uses machine learning.**

Task 6 adds the Historical Data Pipeline (`src/data/historical_pipeline.py`)
for provider adapters, validation, normalization, Parquet persistence, and
incremental updates.
Task 7 adds a real-market validation campaign runner
(`scripts/run_validation_campaign.py`) that executes the completed pipeline
on available real M1 datasets and exports the institutional research report
set.
Task 7.1 adds automated Dukascopy acquisition
(`scripts/download_history.py`) with cached date-range downloads, resume,
retries, metadata cataloguing, tick normalization, and optional tick-to-M1
aggregation.

## Project structure

```
forex-smc-quant/
  config/               settings.py — timezone map, swing/structure/resample + feature-engine configs
  data/
    raw/                 untouched source files
    processed/           cleaned parquet output from the loader + feature-engine datasets
  src/
    data/                loader.py (legacy CSV/Parquet validation),
                          historical_pipeline.py (provider adapters,
                          validation, normalization, incremental updates),
                          resample.py (M1->M5/M15)
    structure/           swings.py (confirmed swing detection), market_structure.py (BOS/CHoCH)
    features/            displacement, order_blocks, fvg, liquidity, sessions,
                          reference_levels, engulfing, confluence, storage.py
    engine/              event_bus.py, incremental.py (trackers), registry.py,
                          engine.py (IncrementalEngine orchestrator) — Task 2.5
    strategies/          context.py (MarketContext), common.py (Signal/confidence),
                          s1_monday_gap .. s5_asian_range_sweep.py, runner.py — Task 3
    backtest/            trade.py, entry.py, stop_loss.py, take_profit.py, execution.py,
                          risk.py, management.py, engine.py, metrics.py, portfolio.py,
                          walkforward.py, reporting.py — Task 4
    research/            experiment.py, parameter_sweep.py, sensitivity.py, market_conditions.py,
                          strategy_analysis.py, symbol_analysis.py, session_analysis.py,
                          confidence_analysis.py, filter_analysis.py, portfolio_research.py,
                          walkforward_research.py, reporting.py, visualizations.py — Task 5
    analytics/           empty — reserved for later tasks
    utils/               timeutils.py — UTC-first timezone helpers
  tests/                 pytest suite + synthetic-data helpers
  scripts/               validate_structure.py (chart + signal overlay),
                          generate_feature_datasets.py (parquet export),
                          benchmark_confluence.py (old vs incremental),
                          visualize_backtest.py (trade-level validation chart)
  docs/MARKET_STRUCTURE_SPEC.md   full swing/BOS/CHoCH specification
  docs/SMC_FEATURE_ENGINE.md      full feature-engine specification (Task 2)
  docs/CONFLUENCE_ENGINE.md       incremental engine architecture (Task 2.5)
  docs/STRATEGY_ENGINE.md         strategy engine specification (Task 3)
  docs/BACKTEST_ENGINE.md         backtest & execution simulator specification (Task 4)
  docs/RESEARCH_LAB.md            research & optimisation laboratory specification (Task 5)
  docs/HISTORICAL_DATA_PIPELINE.md historical data pipeline specification (Task 6)
  docs/REAL_MARKET_VALIDATION_CAMPAIGN.md Task 7 validation campaign workflow
  docs/AUTOMATIC_DATA_DOWNLOADER.md automated Dukascopy downloader workflow
  reports/               generated HTML validation charts land here
```

## Install

```bash
pip install -r requirements.txt
```

## Run tests

```bash
cd forex-smc-quant
python -m pytest -q
```

All 139 tests currently pass: Task 1 (swing detection, BOS/CHoCH
classification, wick-vs-close behaviour, duplicate-event prevention,
resampling, malformed/duplicate/missing-interval data handling), Task 2
(displacement, Order Block creation/mitigation/invalidation, FVG
creation/partial-fill/full-mitigation, liquidity sweeps/equal highs-lows,
sessions/DST, PDH-PDL/weekend gaps, engulfing, confluence, object
lifecycle transitions, and explicit look-ahead-safety checks), Task 2.5
(incremental engine correctness vs. the batch reference implementations,
registry/lifecycle consistency, event-bus ordering with no duplicate/
missing events, confluence snapshot determinism and immutability,
save/load restart-recovery equivalence, and no-look-ahead guarantees),
Task 3 (gap/BOS-pair detection, confidence scoring, reason codes,
signal schema, dedup, configuration overrides/suppression,
reproducibility, and no-look-ahead across truncated vs. full histories),
and Task 4 (entry/stop-loss/take-profit methods, spread/slippage/
commission, position sizing with no-Martingale verification, risk-limit
gating, breakeven/trailing-stop/max-duration/daily-limit management,
full trade simulation to stop/target/partial-exit/expiry, performance
metrics, equity curve/drawdown, portfolio comparison/correlation,
walk-forward splitting, and no-look-ahead/reproducibility checks), and
Task 5 (experiment reproducibility, grid + coordinate parameter sweeps,
causal market-condition classification, symbol/session/confidence/filter
analysis, portfolio-combination generation, rolling walk-forward windows,
sensitivity curves, Calmar ratio, dataset export correctness in all five
formats, and every visualisation function).
Note: `tests/test_strategies.py` runs the full strategy engine over a
10-day synthetic dataset several times and takes ~3-4 minutes on its own
in this environment — see the Task 3 performance note below.
`tests/test_research.py` similarly drives the full Task 3 -> Task 4
pipeline for its integration-level tests and takes ~2 minutes. All Task 4
tests run in under 2 seconds (they construct signals/trades directly
rather than driving the full pipeline).

## Supplying EUR/USD M1 historical data

Prefer the historical pipeline for real provider exports:

```python
from src.data.historical_pipeline import build_standard_dataset

dataset = build_standard_dataset(
    "data/raw/EURUSD_M1.csv",
    provider="dukascopy",
    symbol="EURUSD",
    timeframe="M1",
    source_tz="UTC",
)
dataset.data.to_parquet("data/processed/EURUSD_M1.parquet", index=False)
```

The legacy `src.data.loader.load_m1_csv` helper remains available for
simple CSV/Parquet validation, but the historical pipeline is the
preferred entry point for institutional-quality data.

Provide a CSV (or Parquet) file with at minimum these columns:

```
timestamp, open, high, low, close
```

Optional columns: `volume`, `bid`, `ask`, `spread`.

- `timestamp` should be ISO-8601. If it has no UTC offset/timezone info,
  you MUST tell the loader the source timezone explicitly via
  `--source-tz` (e.g. `UTC`, or your broker's server timezone) — the loader
  refuses to silently assume Nigerian time, broker time, or any other
  zone.
- Place the file under `data/raw/`, e.g. `data/raw/EURUSD_M1.csv`.
- A synthetic example file was generated for validation at
  `data/raw/EURUSD_M1_synthetic.csv` (2,000 M1 candles, randomly walked
  with an injected sine-wave trend component so BOS/CHoCH events actually
  occur). This is placeholder data only — replace it with a real broker
  export (e.g. Dukascopy, HistData.com, or your MT4/MT5 export) before
  drawing any real conclusions.

## Running structure analysis + visual validation

```bash
python scripts/validate_structure.py \
  --input data/raw/EURUSD_M1.csv \
  --symbol EURUSD \
  --timeframe 15min \
  --source-tz UTC \
  --left 2 --right 2 \
  --out reports/eurusd_validation.html
```

Omit `--out` to open the interactive Plotly chart directly instead of
saving to HTML. The chart shows candlesticks, confirmed swing highs/lows,
BOS/CHoCH markers, Order Blocks, FVGs, liquidity levels, PDH/PDL/PWH/PWL,
session highs/lows, and weekend gaps. Every category is its own legend
entry — click to toggle it on/off — so you can visually confirm the
algorithm is not using future information and is not over/under-detecting
structure or SMC objects.

See [docs/MARKET_STRUCTURE_SPEC.md](docs/MARKET_STRUCTURE_SPEC.md) for the
full swing/BOS/CHoCH definitions and [docs/SMC_FEATURE_ENGINE.md](docs/SMC_FEATURE_ENGINE.md)
for the full feature-engine specification.

## Generating feature-engine datasets

```bash
python scripts/generate_feature_datasets.py \
  --input data/raw/EURUSD_M1.csv --symbol EURUSD --timeframe 15min
```

Writes `order_blocks.parquet`, `fvgs.parquet`, `liquidity.parquet`,
`sessions.parquet`, `reference_levels.parquet`, `weekend_gaps.parquet`,
`engulfing.parquet`, and `confluence.parquet` to `data/processed/`.

## Incremental engine (Task 2.5)

```python
import pandas as pd
from src.engine.engine import IncrementalEngine

engine = IncrementalEngine(symbol="EURUSD", timeframe="M1", interval=pd.Timedelta(minutes=1))
snapshots = engine.process_dataframe(df)   # df: timestamp, open, high, low, close

engine.save("state/eurusd_m1.json")        # persist for restart/recovery
resumed = IncrementalEngine.load("state/eurusd_m1.json")
```

`engine.registry` holds the current Active Object Registry;
`engine.event_bus.log` holds every event published so far. See
[docs/CONFLUENCE_ENGINE.md](docs/CONFLUENCE_ENGINE.md) for the full
architecture, and benchmark it yourself:

```bash
python scripts/benchmark_confluence.py --num-candles 150 --incremental-only-candles 100000
```

## Strategy Engine (Task 3)

```python
from src.strategies.context import MarketContext
from src.strategies.runner import run_strategies, save_signals

context = MarketContext(symbol="EURUSD", m1=df)   # df: timestamp, open, high, low, close
signals = run_strategies(context)                  # runs all 5 enabled strategies once
save_signals(signals, "data/processed/signals.parquet")
```

- **S1** — Monday Gap Reversion
- **S2** — Third BOS Continuation
- **S3** — Liquidity Sweep Reversal
- **S4** — Previous-Day High/Low Sweep
- **S5** — Asia-to-London Liquidity Sweep

Every strategy is independently configurable (`config.settings.S1Config`
.. `S5Config`, including an `enabled` flag) and returns the same `Signal`
schema. See [docs/STRATEGY_ENGINE.md](docs/STRATEGY_ENGINE.md) for the
full specification, confidence-scoring formula, and known limitations.
**This engine detects and records signals only — it does not execute
trades, size positions, or manage risk.**

Visualize signals on the chart:
```bash
python scripts/validate_structure.py --input data/raw/EURUSD_M1.csv \
  --symbol EURUSD --signals --strategies S3,S5 --min-confidence 80 \
  --out reports/signals.html
```

**Performance note**: this environment's pandas defaults string columns
to a pyarrow-backed dtype, which made naive per-candle DataFrame
filtering pathologically slow (a profiled bug, fixed by caching engine
output as plain Python records for point-in-time lookups — see
docs/STRATEGY_ENGINE.md). Strategy scans remain O(events × candles in
each event's window) in the worst case; scanning a full multi-year M1
history strategy-by-strategy is a reasonable next optimization target
(see the Task 3 final report's architectural recommendations).

## Backtesting & Execution Simulator (Task 4)

```python
from src.backtest.engine import run_backtest
from src.backtest.reporting import generate_all_reports
from src.backtest.portfolio import compare_strategies
from config.settings import DEFAULT_RISK_CONFIG

trades = run_backtest(signals, m1, context=context)   # signals from Task 3, m1 = the same OHLCV df
generate_all_reports(trades, DEFAULT_RISK_CONFIG.starting_balance, "data/processed/backtest")
# -> backtest_results.parquet, trade_history.parquet, equity_curve.parquet, performance_summary.json

by_strategy = {}
for t in trades:
    by_strategy.setdefault(t.strategy_id, []).append(t)
comparison = compare_strategies(by_strategy, DEFAULT_RISK_CONFIG.starting_balance)
```

Configurable entry (market/OB-touch/midpoint/proximal/distal/confirmation-close),
stop-loss (structural/OB-extreme/fixed-pips/ATR/percentage), take-profit
(fixed R:R/previous-high-low/liquidity-level/gap-fill-25-50-75-100/next-BOS,
with partial scaling), realistic execution (spread/slippage/commission/
latency), risk management (4 sizing methods, portfolio-level daily/
weekly-loss/consecutive-loss/exposure limits — **no Martingale, no
Grid**), and trade management (breakeven/trailing-stop/max-duration/
session-close/daily-trade-limit) are all configured via
`config.settings.{Entry,StopLoss,TakeProfit,Execution,Risk,Management}Config`
— see [docs/BACKTEST_ENGINE.md](docs/BACKTEST_ENGINE.md) for the full
specification, trade lifecycle, and known limitations.

Visualize simulated trades on the chart:
```bash
python scripts/visualize_backtest.py --input data/raw/EURUSD_M1.csv \
  --symbol EURUSD --winners-only --strategies S3,S5 --min-confidence 80 \
  --out reports/backtest_validation.html
```

## Research & Optimisation Laboratory (Task 5)

```python
from src.strategies.context import MarketContext
from src.research.experiment import run_experiment
from src.research.parameter_sweep import coordinate_sweep
from src.research.portfolio_research import analyze_portfolio_combinations
from src.research.reporting import save_research_datasets

context = MarketContext(symbol="EURUSD", m1=m1)          # build ONCE, reuse across every experiment
baseline = run_experiment("baseline", "EURUSD", m1, context=context)

# greedy, non-brute-force parameter search (see docs/RESEARCH_LAB.md)
result = coordinate_sweep("EURUSD", m1, context, configs, param_specs)

by_strategy = {...}  # {strategy_id: [Trade, ...]} from run_backtest, grouped
portfolio_df = analyze_portfolio_combinations(by_strategy)   # every single/pair/triple/all-5 combo

save_research_datasets("data/processed/research", [baseline], portfolio_analysis=portfolio_df)
```

Answers *why* a strategy performs the way it does, not just whether it
was profitable: parameter sweeps (grid + greedy coordinate search),
market-condition (trending/ranging, volatility, bull/bear, gap days)
analysis, symbol/session/confidence/filter effectiveness breakdowns,
every strategy-combination portfolio test with diversification benefit,
rolling walk-forward windows, and parameter sensitivity curves. See
[docs/RESEARCH_LAB.md](docs/RESEARCH_LAB.md) for the full architecture,
experiment reproducibility guarantees, and known limitations (news-day
classification is a documented placeholder; no live economic calendar is
integrated).
