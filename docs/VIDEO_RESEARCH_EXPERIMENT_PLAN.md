# Proposed Research Experiment — Video Methodology Applied to SozoFXSMC (Task 11.5 Phase 6/7)

**Status: DESIGN ONLY. Not executed. No code changes. Requires explicit go-ahead before any of this runs.**

Target research question, adapted from the video's own framing (methodology doc §27): *"Does the historical data provide evidence that Sunday/Monday gap + liquidity + sweep + FVG + market structure + session timing produces a repeatable Forex trading edge after realistic costs and statistical validation — not assuming it does, and explicitly correcting for having tested many configurations?"*

## Dataset

- Source: existing `src/data/historical_pipeline.py` / Dukascopy adapter (already validated, Task 8) — no new data source needed.
- Resolution: M1, resampled to M5/M15 via the existing `src/data/resample.py` (matches what S1-S5 already use — no new resampling logic needed).
- Coverage: whatever full-history range is currently cached per symbol (verify actual date range at execution time via `discover_dataset_specs` in `src/research/validation_campaign.py` rather than assuming a fixed window here) — do not fabricate a specific date range in this design doc.

## Symbols

- Primary: EURUSD (has the deepest history in this platform).
- Secondary, for cross-symbol robustness (Phase 5's "symbol dependence" audit point): GBPUSD, USDJPY, AUDUSD, USDCHF, USDCAD, NZDUSD — the same 7-pair set already used by the live infrastructure (Task 11).
- A configuration that only "works" on one symbol is a red flag per this audit's own findings (§9 of the statistical audit) — cross-symbol stability is a REQUIRED robustness check, not optional.

## Time period / split (walk-forward structure)

Reuses existing infrastructure (`src/backtest/walkforward.py`, `src/research/walkforward_research.py`) — no new splitting logic needs to be built:

- **Training period**: first 60% of available history per symbol (`split_dataset(train_pct=0.6)` default already matches this).
- **Validation period**: next 20% (`validation_pct=0.2` default) — used for parameter selection/ranking ONLY, never for final performance claims.
- **Out-of-sample (holdout) period**: final 20%, touched exactly once, only after the candidate configuration(s) are frozen from training+validation. No re-tuning after looking at this period.
- **Rolling walk-forward, within the training+validation window only**: `generate_rolling_windows(test_days=5, step_days=5, train_days=20)` (existing defaults) to additionally check stability across sub-periods before ever touching the holdout.

## Timeframes

- Structure/CHoCH confirmation: M5 (matches existing S1/S3/S4/S5 convention — no reason to deviate without evidence).
- Order Block / FVG timeframe: M15 (matches existing convention).
- Entry-candle resolution: M1 (matches existing convention).
- Rationale: reuse the platform's already-validated timeframe conventions rather than introducing a new set the video's reconstruction doesn't actually specify (see UNKNOWN in methodology doc).

## Candidate features (reusing existing detectors, not rebuilding them)

- Weekend gap: `src/features/reference_levels.py` (already computes weekend gaps)
- Liquidity levels + sweep: `src/features/liquidity.py`
- Displacement: `src/features/displacement.py`
- FVG: `src/features/fvg.py`
- Order Blocks: `src/features/order_blocks.py`
- Structure (BOS/CHoCH): `src/structure/market_structure.py`
- Sessions: `src/features/sessions.py`

## Parameter ranges (the actual "dials," per the video's parameterization philosophy)

| Parameter | Candidate values | Config field to sweep |
|---|---|---|
| Gap size threshold | 0.05%, 0.10%, 0.15%, 0.20%, 0.30%, 0.50%, 1.00% | `S1Config.min_gap_size` (currently absolute price; needs a %-of-price variant added, or convert % to per-symbol absolute at sweep-generation time — NO existing code change required to design this, only to execute it) |
| Gap direction | bullish, bearish, both | new filter, not currently a toggle |
| Gap-fill target | 25%, 50%, 75%, 100% | `TakeProfitConfig` — needs a `gap_fill_X` variant beyond the current `gap_fill_25/50/75/100` (these already exist!) |
| Liquidity reference | prev-week high/low, Friday high/low, Asian session high/low, London session high/low | `LiquidityConfig` + `SessionConfig` (already support session-scoped highs/lows) |
| Liquidity sweep | required / not required | `S1Config`-style boolean toggle (pattern already used elsewhere, e.g. `S3Config.require_displacement`) |
| FVG | required / optional, min size, retracement depth | `FVGConfig.min_gap_size` (exists) + retracement-depth (NEW field, not currently in `FVGConfig`) |
| Order Block | required / optional | `require_fresh_ob` (already exists on every S-config) |
| Entry | immediate, FVG retracement (25/50/75%), OB entry | `EntryConfig.method` (mostly exists; FVG-retracement-% entry is NEW) |
| Stop loss | beyond sweep, beyond structure, ATR, fixed % | `StopLossConfig.method` (exists) |
| Take profit | 1R-3R, gap midpoint, full gap fill, opposing liquidity | `TakeProfitConfig.method` (mostly exists) |
| Session | Asian, London, New York, London/NY overlap, all day | `session_filter` (exists on every S-config) |
| Holding period cap | 15m, 30m, 1h, 2h, 4h, end-of-session, end-of-day | `ManagementConfig.max_trade_duration_candles` / `session_close_exit` (exist) |

## Number of combinations

Full cartesian product of the table above is large (7 gap sizes x 3 directions x 4 fill targets x ~6 liquidity refs x 2 sweep-required x ~6 FVG variants x 2 OB-required x ~7 entry x ~4 stop x ~9 TP x 5 session x 7 holding ≈ **low tens of millions**, same order of magnitude as the video's reported 258M space). Per the video's own approach (and this audit's multiple-testing findings), we should NOT exhaustively test this — **sample a bounded number, on the order of 1,000-5,000 configurations** (smaller than the video's 25,000, deliberately, given our smaller data footprint and the need to leave enough out-of-sample trades per configuration for the statistical requirements below), via random or Latin-hypercube sampling of the parameter grid (existing `src/research/parameter_sweep.py::grid_sweep`/`coordinate_sweep` need a random-sampling mode added — currently exhaustive/coordinate-descent only).

## Transaction costs and slippage

Use the EXISTING `ExecutionConfig` unchanged (spread_pips=1.0, commission_per_lot=7.0, slippage_pips=0.5, applied via `src/backtest/execution.py`) — do not weaken these for the sake of making more configurations look profitable. Per the statistical audit (§7), this is one area where Forex-specific realism matters and the platform already has a validated cost model; no need to invent a new one.

## Minimum trade count

**No candidate configuration may be reported/ranked with fewer than 30 closed trades in the OUT-OF-SAMPLE period.** (30 is a conventional minimum for the CLT-based Sharpe/expectancy estimates already computed in `src/backtest/metrics.py` to be even approximately reliable; below this, report "insufficient sample" rather than a metric value — consistent with the honesty principle already applied throughout this platform's own reporting, e.g. `src/live/journal.py`'s insufficient-history handling.)

## Statistical significance requirements

1. Per-configuration: a one-sided t-test (or bootstrap CI) on the OUT-OF-SAMPLE per-trade PnL, testing H0: mean PnL <= 0 (after costs).
2. Family-wise: apply a multiple-testing correction across however many configurations are actually tested (see next section) — do not rank/select using raw uncorrected p-values.
3. Preferred: compute a **Deflated Sharpe Ratio / Probability of Backtest Overfitting (PBO)** estimate (Bailey et al. framework) across the full set of tested configurations, not just the winner — this directly answers "how many of the trials we ran would be expected to look this good by chance." **This does not exist in SozoFXSMC today and would need to be built** (flagged in the statistical audit as a Category C gap) — a bounded, well-specified addition, not a full new subsystem.

## Multiple-testing correction

- Minimum bar: report both the raw (uncorrected) and Bonferroni-corrected significance for every surviving configuration, so the difference is never hidden.
- Preferred bar: PBO/deflated-Sharpe as above, which correctly accounts for the correlation between similar configurations (Bonferroni alone over-penalizes here, per the statistical audit §1).
- No configuration should be called "validated" on the strength of in-sample/validation performance alone — the OUT-OF-SAMPLE holdout result is the only number that counts for the final claim, exactly as this platform's own paper-trading/forward-testing work (Task 11) already insists on for live deployment claims.

## Robustness tests (before ANY configuration is called a candidate)

1. **Out-of-sample holdout** (see Time period above) — mandatory, single-pass, no re-tuning after viewing.
2. **Walk-forward stability** across rolling windows within train+validation (`summarize_stability` already exists in `src/research/walkforward_research.py`).
3. **Parameter sensitivity / stability region**: for every surviving configuration, perturb each parameter by one grid-step in each direction and confirm performance doesn't collapse (`src/research/sensitivity.py::detect_diminishing_returns` — reusable, needs a "stability region" wrapper, not a rebuild).
4. **Cross-symbol stability**: the same configuration re-tested on all 7 pairs; a configuration that only works on one symbol is downgraded, not promoted (per statistical audit §9).
5. **Cross-regime stability**: re-tested against `src/research/market_conditions.py` regime labels (trending/ranging, high/low volatility) — already available, not new.
6. **Monte Carlo / trade-sequence randomization**: shuffle the order of the out-of-sample trade P&Ls and confirm the equity curve's drawdown/ruin characteristics aren't a lucky ordering artifact. **This does not exist in SozoFXSMC today and must be built** — a self-contained, bounded new module (Category C gap).
7. **Cost-sensitivity check**: re-run the winning configuration at 1.5x and 2x the default spread/slippage assumptions; a configuration whose edge disappears under mildly worse costs is fragile.

## Phase 7 — Baseline comparison bar

The new methodology's output must be compared against our EXISTING baseline before any adoption decision:

**Baseline = S3, S4, S3+S4 combined, with IOS-based selection and ITQS filtering (Tasks 8-10's already-validated pipeline).**

Minimum metrics to compare, all computed via the existing `src/backtest/metrics.py` (Sharpe/Sortino already implemented; Calmar is not currently a named field and would need a one-line addition — `net_profit_annualized / max_drawdown`):

| Metric | Source |
|---|---|
| Expectancy | `compute_performance_metrics()["expectancy"]` |
| Profit factor | `compute_performance_metrics()["profit_factor"]` |
| Win rate | `compute_performance_metrics()["win_rate"]` |
| Maximum drawdown | `compute_performance_metrics()["max_drawdown_pct"]` |
| Sharpe | `compute_performance_metrics()["sharpe_ratio"]` |
| Sortino | `compute_performance_metrics()["sortino_ratio"]` |
| Calmar | NOT currently computed — needs a one-line addition (`net_profit / max_drawdown`, annualized) |
| Number of trades | `compute_performance_metrics()["signal_utilization"]["closed_trades"]` |
| Stability across symbols | `src/research/symbol_analysis.py` (exists) |
| Stability across regimes | `src/research/market_conditions.py` + existing per-regime expectancy breakdown |
| Out-of-sample performance | New experiment's holdout metrics vs. baseline's own out-of-sample numbers (Task 8/10 already report these) |
| Walk-forward performance | `summarize_stability()` output, both methodologies |

**Adoption bar**: a candidate from the new methodology should not be adopted unless it BEATS the S3+S4+IOS+ITQS baseline on expectancy AND max-drawdown AND out-of-sample stability simultaneously — beating on a single metric (e.g., higher win rate alone) is explicitly insufficient, per the video's own stated lesson (methodology doc §13, §23) and this platform's existing "no optimize-for-win-rate-alone" principle already embedded in `src/decision_engine/ios.py`'s multi-factor design.

## What this experiment explicitly does NOT do

- Does not modify S3, S4, ITQS, IOS, the Decision Engine, or the Paper Broker.
- Does not create live trades.
- Does not touch Telegram or GitHub Actions.
- Does not run yet — this is a design document pending explicit approval.
