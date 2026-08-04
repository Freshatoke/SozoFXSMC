# Strategy Engine Specification (v1.0, Task 3)

This document specifies `src/strategies/` -- five independent research
strategies built entirely on top of the Task 1 Market Structure Engine
and Task 2 SMC Feature Engine. **This module detects, validates, and
records signals. It does not execute trades, size positions, manage risk,
or optimize anything.**

## Architecture

```
src/strategies/
  context.py     -- MarketContext: computes swings/structure/OB/FVG/
                     liquidity/sessions/reference levels/engulfing ONCE
                     per timeframe, shared by every strategy (no strategy
                     re-implements detection)
  common.py      -- Signal schema, confidence scoring, reason codes,
                     dedup helpers (identical across all 5 strategies)
  s1_monday_gap.py, s2_third_bos.py, s3_liquidity_sweep.py,
  s4_pdh_pdl_sweep.py, s5_asian_range_sweep.py
                 -- one module per strategy; each only SEQUENCES
                    already-detected objects, never detects them itself
  runner.py      -- orchestrates enabled strategies over one shared
                     context, dedupes, exports signals.parquet
```

Every strategy module receives the same `MarketContext` and its own
config dataclass (`config.settings.S1Config` .. `S5Config`). Disabling a
strategy (`enabled=False`) or changing its thresholds never affects any
other strategy -- they don't share state beyond the read-only context.

## MarketContext

`MarketContext.__init__(symbol, m1, ...)` takes only the M1 OHLCV
DataFrame; M5/M15 candles are resampled lazily (`src.data.resample`) and
cached. Every other query -- `swings(tf)`, `structure_events(tf)`,
`order_blocks(tf)`, `fvgs(tf)`, `liquidity(tf)`, `sessions`,
`reference_levels`, `weekend_gaps`, `engulfing` -- is computed exactly
once per timeframe (lazily, memoized) using the Task 1/2 reference
implementations directly. Running all five strategies over one context
therefore computes each engine only once, not five times.

Convenience helpers used by every strategy:
- `structure_state_asof(tf, t)` / `latest_choch_asof(tf, t, direction)`
- `fresh_order_block_asof(tf, direction, t, price_near=None)`
- `active_fvg_asof(tf, direction, t)`
- `session_active_asof(session_name, t)`

**Performance note**: this environment's pandas defaults string columns
to a pyarrow-backed dtype, which made repeated per-candle boolean-mask
filtering pathologically slow (profiled at >85% of runtime). The `asof`
helpers therefore convert each engine's output to a plain Python list of
dicts exactly once (cached) and do point-in-time lookups as plain Python
loops/comparisons against that list, never touching pandas/pyarrow inside
a strategy's per-candle scan.

### A look-ahead bug found and fixed while building this

`order_blocks(tf)`/`fvgs(tf)` are computed ONCE over the FULL history, so
their `current_state`/`freshness_status`/`active_status` columns describe
each object's FINAL outcome (as of the end of the dataset) -- not its
state at an arbitrary earlier `timestamp`. An early version of
`fresh_order_block_asof` filtered on `freshness_status == "FRESH"`
directly, which is look-ahead bias: an OB that gets touched next week
would wrongly be excluded from a "still fresh today" check made this
week. The fix uses `first_touch_timestamp` (a fixed, immutable fact about
exactly when, if ever, the zone was first touched) and compares it
against the query timestamp instead -- "was it touched by `t`" only
depends on candles up to `t`, regardless of what the full dataset knows
about later candles. `active_fvg_asof` has no equivalent timestamp field
in `src.features.fvg`'s output, so it instead recomputes FVGs fresh with
the batch engine's own `as_of_index` cutoff (the same mechanism Task 2's
confluence engine uses) -- accepted since it is only invoked when a
strategy opts into `require_fvg=True` (off by default).

## The five strategies

### S1 -- Monday Gap Reversion
Detects a weekend gap (`MarketContext.weekend_gaps`), then requires, in
order: price beginning to move back toward the Friday close, an M5 CHoCH
in the reversal direction after the gap reopened, an M1 CHoCH in the
reversal direction confirmed exactly at the entry candle, a fresh Order
Block in the reversal direction, and (optional) a matching engulfing
candle. Target is always the Friday close (the gap-fill level). Records
25/50/75/100% fill checkpoints, MAE/MFE, and time-to-fill as **post-hoc
research statistics** -- computed by looking forward from the entry, but
never used to decide whether the signal fires.

### S2 -- Third BOS Continuation
Finds two consecutive same-direction BOS events on `bos_timeframe`
(default M15; the structure engine's own state machine guarantees no
opposing CHoCH occurred between them). After the second BOS, requires a
fresh retracement Order Block, an M5 CHoCH, and an M1 CHoCH, all in the
continuation direction. Target is `measured_move` (project the same
breakout distance again) or `liquidity` (nearest opposing liquidity level
beyond price), per `config.target_style`. Records whether a third BOS
actually occurred afterward (`metadata.bos3_occurred`) as a post-hoc
research outcome.

### S3 -- Liquidity Sweep Reversal
For every swept liquidity level (`MarketContext.liquidity`), requires
displacement in the reversal direction, then a CHoCH, then a fresh Order
Block, with optional FVG alignment.

### S4 -- Previous Day High/Low Sweep
Detects a sweep of PDH/PDL (`MarketContext.reference_levels`) using the
same wick-beyond/close-back rule `src.features.liquidity` already defines
for swing-based levels (Task 2 does not itself compute PDH/PDL sweeps),
then CHoCH, then a fresh Order Block.

### S5 -- Asia-to-London Liquidity Sweep
Uses the Asian (Tokyo) session's high/low (`MarketContext.sessions`),
detects a sweep after the session ends, requires CHoCH and a fresh Order
Block, and (`config.session_filter`, default `("london",)`) restricts the
entry candle to the London session.

## Common Signal output

Every strategy returns `src.strategies.common.Signal` instances with
identical fields: `signal_id, strategy_id, timestamp, symbol, timeframe,
direction, entry_zone, stop_loss_reference, target_reference,
confidence_score, reason_codes, confluence_snapshot,
market_structure_state, session, risk_reference, metadata`.

`signal_id` format: `{strategy_id}_{symbol}_{timeframe}_{YYYYMMDDTHHMMSS}_{seq}`.
The trailing `seq` is a per-call enumeration counter -- an implementation
detail of iteration order, not part of a signal's semantic identity (see
the no-look-ahead test's docstring in `tests/test_strategies.py` for why
comparing raw ids across two different runs can be misleading).

## Confidence scoring

Deterministic, rule-based, fully explainable -- **no machine learning**.
`compute_confidence(factor_values, weights=DEFAULT_FACTOR_WEIGHTS)`
takes `{factor_name: value_in_[0,1]}` (only the factors a strategy
actually evaluates) and returns a weighted average scaled to 0-100,
normalized by the sum of weights actually supplied (so a strategy that
doesn't check, say, FVG alignment is never penalized for that factor's
weight). Universal factor weights:

| Factor | Weight |
|---|---|
| FreshOrderBlock | 0.20 |
| CHoCHConfirmation | 0.20 |
| DisplacementQuality | 0.15 |
| LiquiditySweep | 0.15 |
| GapQuality | 0.15 |
| FVGAlignment | 0.10 |
| TrendAlignment | 0.10 |
| Engulfing | 0.05 |
| SessionContext | 0.05 |

Every contributing factor's `{weight, value, contribution}` is stored in
`Signal.metadata["confidence_contributions"]` -- a researcher can always
see exactly why a signal scored the way it did. `config.confidence_threshold`
suppresses (never just down-ranks) signals scoring below it.

## Reason codes

`build_reason_codes(strategy_id, condition_codes, confidence)` returns
`[strategy_id, *condition_codes, f"Confidence{round(confidence)}"]`,
matching the task brief's examples exactly, e.g.
`["S1", "GapDetected", "GapAboveMinimum", "BullishCHoCH", "BullishOrderBlock", "Confidence82"]`.

## Signal lifecycle

A signal is a point-in-time record, not a stateful object -- it is
produced once (when all entry conditions align) and never updated
afterward. `metadata` may carry post-hoc outcome fields (S1's fill
checkpoints/MAE/MFE, S2's `bos3_occurred`) computed by looking forward
from the signal's timestamp; these are research annotations, not part of
the entry decision, and never retroactively change whether the signal
exists (see `test_no_lookahead_bias_...` in `tests/test_strategies.py`).
`dedupe_signals` (used by `runner.run_strategies` and available to every
strategy directly) is a final safety net against duplicate
`(strategy_id, symbol, timeframe, timestamp, direction)` tuples.

## Signal dataset

`src.strategies.runner.save_signals(signals, path)` writes
`signals.parquet` with one row per signal; the required minimum fields
(ID, strategy, timestamp, direction, confidence, reason codes, confluence
snapshot, supporting references) are all present. **Outcome (win/loss,
R-multiple, etc.) is deliberately NOT included** -- that belongs to a
future backtesting task.

## Configuration

Every strategy has its own frozen dataclass in `config/settings.py`
(`S1Config` .. `S5Config`) with an `enabled` flag plus every toggle named
in the task brief: minimum gap size, engulfing/FVG/liquidity requirement
flags, OB freshness requirement, session filter, confidence threshold,
CHoCH/BOS timeframe, target style, stop reference. No code change is
needed to reconfigure a strategy -- construct a new config instance and
pass it via `run_strategies(context, configs={"S1": S1Config(...)})`.

## Visual validation

```bash
python scripts/validate_structure.py --input data/raw/EURUSD_M1.csv \
    --symbol EURUSD --signals --strategies S3,S5 --min-confidence 80 \
    --direction bullish --out reports/signals.html
```

`--signals` computes and overlays signal markers (triangle-up for
bullish, triangle-down for bearish, one color per strategy, hover text
shows reason codes/confidence/entry-stop-target). `--strategies`,
`--min-confidence`, and `--direction` filter which signals are drawn;
every strategy's markers are also their own legend entry, toggleable like
every other category in the chart.

## Known limitations

- **Liquidity clustering vs. look-ahead**: `src.features.liquidity`'s
  equal-level clustering (Task 2, unchanged here) sorts swings by PRICE
  rather than time, so a much-later swing can retroactively merge into an
  earlier level's cluster and shift its averaged price. This means S3/S4/S5
  signals that depend on liquidity levels can, in rare cases, differ
  depending on how much future data the batch engine sees -- a
  pre-existing Task 2 batch-engine property, not something Task 3
  introduces. Set `LiquidityConfig(equal_level_tolerance=0.0)` to disable
  clustering entirely for strict reproducibility research.
- **FVG point-in-time checks are recomputed, not cached**: because
  `src.features.fvg` doesn't record a mitigation timestamp,
  `active_fvg_asof` recomputes FVGs with a fresh `as_of_index` cutoff on
  every call. This is only paid for when `require_fvg=True` (off by
  default in every strategy).
- **One signal per setup**: each strategy stops scanning (`break`) at the
  first candle satisfying all conditions per gap/BOS-pair/sweep/level.
  This is a deliberate simplification (matches "no duplicate signals");
  a setup that "almost" qualifies earlier and then qualifies again later
  under different conditions is not currently re-evaluated.
- **No cross-strategy interaction**: if two strategies would fire
  opposite-direction signals on the same candle, both are recorded
  independently -- reconciling conflicting signals is a future task's
  concern (this module explicitly does not execute trades).
- Asian session = Tokyo window only (same limitation as Task 2).

## Future improvements

- Replace `context.py`'s batch recomputation with the Task 2.5
  incremental engine so strategies can run in a live/streaming context
  without re-deriving history on every call.
- Add measured-move target calibration using ATR rather than the raw
  first/second BOS distance.
- Extend post-hoc outcome tracking (MAE/MFE/time-to-target) to S3/S4/S5,
  currently only implemented for S1.
