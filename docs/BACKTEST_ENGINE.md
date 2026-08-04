# Backtesting & Execution Simulator Specification (v1.0, Task 4)

This document specifies `src/backtest/` -- a realistic trade-execution
simulator that turns Task 3 signals into a complete, auditable trade
history and institutional-grade performance analytics. **This module
does not trade live, connect to a broker, optimize parameters, or use
machine learning.**

## Architecture

```
src/backtest/
  trade.py         -- Trade dataclass (full lifecycle), TradeStatus, ExitReason
  entry.py         -- Entry Engine: market, ob_touch, ob_midpoint,
                       ob_proximal_edge, ob_distal_edge, confirmation_close
  stop_loss.py     -- Stop-Loss Engine: m5_structural, ob_extreme,
                       fixed_pips, atr_multiple, percentage
  take_profit.py   -- Take-Profit Engine: fixed_rr, previous_high_low,
                       liquidity_level, gap_fill_25/50/75/100, next_bos_target,
                       plus uniform partial-exit scaling
  execution.py     -- spread / slippage / commission / PnL cash-flow model
  risk.py          -- position sizing (4 methods) + RiskTracker (portfolio gates)
  management.py    -- breakeven, trailing stop, max duration, session close,
                       daily trade limit -- pure per-candle functions
  engine.py         -- BacktestEngine: simulate_trade() + run_backtest()
                       orchestrating the full lifecycle
  metrics.py        -- full performance metric suite + expectancy breakdowns
  portfolio.py       -- combine/compare strategies, correlation, portfolio equity
  walkforward.py     -- train/validation/out-of-sample dataset splitting
  reporting.py        -- parquet/json exports + visual dashboard
```

Every method (entry/stop/target/sizing) is a plain function registered
in a small dict (`ENTRY_METHODS`, `STOP_LOSS_METHODS`, `TAKE_PROFIT_METHODS`,
`SIZING_METHODS`) -- adding a new method means registering one more
function, never editing an existing one.

## Trade lifecycle

```
Signal Generated -> Entry Validation -> Trade Open -> Trade Management
-> Trade Close -> Trade Result -> Performance Recording
```

Every signal produces exactly one `Trade` record with a terminal status:
- **CLOSED**: entered and exited (see `ExitReason`: STOP_LOSS,
  TAKE_PROFIT, TRAILING_STOP, BREAKEVEN_STOP, MAX_DURATION,
  SESSION_CLOSE, END_OF_DATA).
- **EXPIRED**: the configured entry method never triggered within
  `EntryConfig.max_wait_candles`.
- **REJECTED**: blocked before entry by a risk-management gate (or the
  daily trade limit) -- `Trade.rejection_reason` records why.

No signal silently disappears from the historical record.

### Per-candle evaluation order (avoiding look-ahead)

Once a trade is OPEN, `engine.simulate_trade` walks forward candle by
candle in this fixed order:
1. Check if THIS candle hits the stop-loss set by PRIOR candles.
2. Check if THIS candle hits any remaining take-profit level (nearest
   first). **If both a stop and a TP could be hit in the same candle,
   the stop is assumed to hit first** -- the standard, conservative
   backtesting assumption when intra-candle order can't be known from
   OHLC data alone.
3. If still open, update MAE/MFE and duration.
4. Evaluate breakeven/trailing-stop rules using THIS candle's own
   high/low -- this updates the stop for the **next** candle's check in
   step 1, never re-checked against this same candle (using a candle's
   own extreme to move the stop and then immediately testing the same
   candle against the new stop would be a subtle look-ahead/optimism
   bias -- verified by `tests/test_backtest.py::test_backtest_engine_no_lookahead`).
5. Check max-duration / session-close forced exits.

## Entry Engine

| Method | Behaviour |
|---|---|
| `market` | Next candle's open after the signal |
| `confirmation_close` | The signal's own triggering candle's close |
| `ob_touch` / `ob_proximal_edge` | First candle touching the OB zone's proximal edge (nearer to price at signal time) |
| `ob_distal_edge` | First candle reaching the OB zone's far edge (deeper retracement) |
| `ob_midpoint` | First candle reaching the zone's midpoint |

Bullish OBs sit below price (proximal = zone high, distal = zone low);
bearish OBs sit above price (proximal = zone low, distal = zone high).
If the condition never triggers within `max_wait_candles`, the trade is
EXPIRED. `entry_buffer_pips` shifts the final entry price slightly
against the trader (a conservative confirmation buffer).

## Stop-Loss Engine

`m5_structural` (nearest M5 confirmed swing, via `MarketContext`),
`ob_extreme` (the signal's OB far boundary), `fixed_pips`,
`atr_multiple` (true-range ATR computed only from candles up to entry),
`percentage`. All add `config.buffer_pips` beyond the raw level where
applicable. Computed **once**, at entry time, using only data up to
`entry_timestamp`.

## Take-Profit Engine

`fixed_rr`, `previous_high_low` (PDH/PDL via `MarketContext`),
`liquidity_level` (nearest active opposing liquidity level),
`gap_fill_25/50/75/100` (uses the signal's own gap metadata --
S1-specific), `next_bos_target` (measured-move projection using the
signal's BOS confluence data -- S2-specific). Methods needing data a
given signal doesn't carry fall back to `fixed_rr` (documented per
function in `take_profit.py`).

**Partial scaling** (`config.partial_exits`, a list of `(r_multiple,
fraction)` pairs) applies uniformly on top of any method: `resolve_take_profit`
always returns an ordered, nearest-first list of `(price, fraction)`
levels summing to 1.0.

## Execution Model

The OHLC series is treated as the **mid price**. The full spread is
charged **once, at entry** (buy at ask = mid + spread; sell-entry at bid
= mid - spread); exits fill at mid — a standard simplified round-trip
spread model. Slippage applies **only** at entry and at stop-loss/
trailing-stop exits (never take-profit, which is a resting limit order).
Commission is `lots x commission_per_lot`, charged per closed lot
(including each partial exit). `latency_candles` delays the sampled fill
by N candles after the trigger is first identified (modeling execution
delay) — this shifts *when* a price is sampled; the price/cash-flow
adjustments themselves are entirely execution.py's concern, kept
independent so each part stays separately testable. Order rejection and
partial fills are explicitly **out of scope** for v1.0 (the task brief
lists both as "future extension").

## Risk Management

Sizing methods: `fixed_lot`, `fixed_percentage_risk` (risk_amount /
(stop_distance x contract_size)), `fixed_monetary_risk`,
`volatility_adjusted` (same formula, halved when current ATR exceeds
1.5x the stop distance). **No Martingale, no Grid**: every method computes
size from the CURRENT balance and CURRENT stop distance only —
`tests/test_backtest.py::test_no_martingale_sizing_does_not_depend_on_prior_losses`
verifies sizing never reads trade history.

`RiskTracker` gates new entries chronologically across the whole signal
stream (not per-signal in isolation): max simultaneous positions, max
daily/weekly loss (% of balance), max consecutive losses, max portfolio
exposure (% of balance at risk across open positions). `run_backtest`
shares one `RiskTracker` across every signal so these limits are genuine
portfolio-level constraints.

## Trade Management

Breakeven (`breakeven_trigger_r`, moves stop to entry + buffer once
favorable excursion reaches N x risk distance), trailing stop
(`fixed_pips`, `atr`, or a simple `structure` trail using the current
candle's own low/high), max trade duration (in candles), session-close
forced exit (via `MarketContext.sessions`), and daily trade limits (caps
new entries per calendar day, checked before entry validation even
begins).

## Performance Metrics (`metrics.py`)

Net/Gross Profit/Loss, Win/Loss Rate, Profit Factor, Expectancy, Average
Winner/Loser, Max Drawdown (absolute and %), Max Consecutive Wins/Losses,
Sharpe & Sortino Ratio (computed on **daily aggregated** realized PnL
returns, annualized with sqrt(252); Sortino uses downside deviation
only), Recovery Factor (Net Profit / |Max Drawdown|), Average Trade
Duration, average MAE/MFE, full R-Multiple distribution (mean/std/min/
max/raw values for histogramming), and expectancy broken down by
strategy, symbol, session, direction, and confidence bucket (10-point
bins). Only `CLOSED` trades count; EXPIRED/REJECTED counts and rejection
reasons are reported separately under `signal_utilization`.

## Portfolio Testing (`portfolio.py`)

`combine_trades({strategy_id: [Trade,...]})` merges any subset — one
strategy ("individual"), several ("any combination"), or all five ("all
combined") — trivially, since "portfolio" here just means "the union of
trades, re-run through the same metrics." `compare_strategies` builds the
required comparison table (best win rate/expectancy/profit factor/lowest
drawdown/session/symbol/confidence range/risk:reward);
`strategy_correlation` computes the Pearson correlation of each
strategy's **daily realized PnL** series (days with no closed trade
contribute zero return) to help judge whether combining strategies
actually diversifies risk.

## Walk-Forward Support (`walkforward.py`)

`split_dataset` splits an M1 DataFrame chronologically **by row count**
(not calendar time, so weekends/holidays don't skew proportions) into
train/validation/out-of-sample. `split_dataframes` returns the three
DataFrames; `tag_trades_by_period` labels each trade's
`metadata["walk_forward_period"]`. Parameter optimization itself is
explicitly out of scope for Task 4 — this only prepares the dataset
architecture for a future optimization task.

## Reporting (`reporting.py`)

Writes `backtest_results.parquet` (slim, flat, numeric-friendly — no
JSON-encoded columns), `trade_history.parquet` (full detail, including
JSON-encoded reason codes / confluence snapshot / management events /
partial exits), `equity_curve.parquet`, and `performance_summary.json`.
`build_dashboard`/`save_dashboard_html` render one self-contained Plotly
HTML with 8 panels: equity curve, drawdown curve, monthly returns, trade
PnL distribution, win/loss counts, MAE vs MFE scatter, R-multiple
histogram, and expectancy by strategy/session.

## Visual Validation

```bash
python scripts/visualize_backtest.py --input data/raw/EURUSD_M1.csv \
    --symbol EURUSD --winners-only --strategies S3,S5 --min-confidence 80 \
    --out reports/backtest_validation.html
```

Draws every filtered trade as a line from entry to exit (green = winner,
red = loser) over the candlestick chart, with a dotted marker down to the
initial stop; hover text shows strategy, direction, entry/exit
price+time, duration, PnL, R-multiple, confidence, and reason codes.
Filters: `--winners-only`/`--losers-only`, `--strategies`, `--symbols`,
`--sessions`, `--min-confidence`, `--start`/`--end`.

## Configuration

`config/settings.py`: `EntryConfig`, `StopLossConfig`, `TakeProfitConfig`,
`ExecutionConfig`, `RiskConfig`, `ManagementConfig` — every method,
threshold, and limit named in the task brief is a field with a sensible
default. No code change is needed to reconfigure the simulator.

## Known limitations

- **Intra-candle ordering**: OHLC data cannot tell us whether price hit
  the stop or the target first within the same candle; the simulator
  always assumes the stop hit first (conservative). A tick-level
  simulation would resolve this exactly, at a much higher data/compute
  cost not justified for v1.0.
- **`trailing_method="structure"`** trails behind the current candle's
  own low/high rather than re-querying `MarketContext.swings` on every
  candle (which would be an O(candles) re-scan cost per open trade per
  candle) — a real swing-based trail is a natural follow-up once
  Task 2.5's incremental swing tracker is wired into the backtest loop.
- **Latency model**: `latency_candles` samples the fill N candles later
  at that candle's open — a simplification of true order-queue latency,
  not a network/broker latency simulation.
- **Order rejection and partial fills**: explicitly out of scope per the
  task brief ("future extension"); every entry that passes risk gates is
  assumed to fill completely at its computed price.
- **Sharpe/Sortino** use daily-aggregated returns, which understates
  their statistical significance on datasets spanning only a few weeks —
  meaningful comparison across strategies requires a reasonably long
  backtest window.
- **Correlation-based portfolio construction** (e.g. actually weighting
  strategies by their correlation) is not implemented — `strategy_correlation`
  reports the matrix; acting on it is a future task.

## Example

```python
from src.strategies.context import MarketContext
from src.strategies.runner import run_strategies
from src.backtest.engine import run_backtest
from src.backtest.reporting import generate_all_reports
from src.backtest.portfolio import compare_strategies
from config.settings import DEFAULT_RISK_CONFIG

context = MarketContext(symbol="EURUSD", m1=m1_df)
signals = run_strategies(context)
trades = run_backtest(signals, m1_df, context=context)

generate_all_reports(trades, DEFAULT_RISK_CONFIG.starting_balance, "data/processed/backtest")

by_strategy = {}
for t in trades:
    by_strategy.setdefault(t.strategy_id, []).append(t)
comparison = compare_strategies(by_strategy, DEFAULT_RISK_CONFIG.starting_balance)
```

## Future extensions

- Order rejection / partial fills (named explicitly in the task brief).
- Structure-based trailing stop wired to Task 2.5's incremental swing tracker.
- Actual walk-forward parameter optimization on top of `walkforward.py`'s splits.
- Multi-symbol portfolio simulation with cross-symbol exposure limits.
