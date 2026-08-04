# SMC Feature Engine Specification (v0.1)

This document specifies every module under `src/features/`, built on top of
the Task 1 Market Structure Engine (`src/structure/`). It follows the same
principle: **every strategy queries these engines; no strategy re-derives
Order Blocks, FVGs, liquidity, sessions, or reference levels itself.**

All engines accept an `as_of_index` parameter. Passing it restricts the
engine to only "know" candles up to and including that index, which is how
look-ahead safety is verified (see `tests/test_no_lookahead.py`): a
snapshot computed with `as_of_index=k` on the full history must be
byte-for-byte identical to running the same engine on a DataFrame
physically truncated to `k+1` rows.

---

## 1. Displacement Engine (`displacement.py`)

A candle qualifies as **impulsive** when at least `min_conditions_met`
(default 2) of these three causal conditions hold:

1. `body_size >= atr_multiplier * ATR(atr_period)`
2. `body_size >= body_multiple * average body size over the prior recent_body_lookback candles` (shifted by one candle so it never includes itself)
3. `body_ratio (body_size / candle_range) >= min_body_ratio`

Consecutive impulsive candles in the same direction are grouped into one
**displacement event**. Every event records each contributing candle's
per-condition pass/fail and raw values (`reasons`) so a human can see
exactly *why* it qualified.

---

## 2. Order Block Engine (`order_blocks.py`)

**Bullish OB** = the final bearish candle immediately preceding qualifying
bullish displacement. **Bearish OB** = the final bullish candle immediately
preceding qualifying bearish displacement. The opposite-direction candle is
searched for up to `lookback_candles` bars before the displacement start;
if none is found, no OB is created (recorded in the `skipped` list with a
reason — nothing fails silently).

### Why mitigation is checked from the END of the displacement run, not its start

The OB candle's very next bar is, by construction of continuous OHLC data,
the first candle of the displacement itself, and its `open` always equals
the OB candle's `close` — which sits inside the OB's own `[low, high]`
range. If mitigation were checked starting there, every single Order Block
would appear "touched" the instant it's created, which is meaningless.
Mitigation is therefore evaluated only on candles *after* the whole
displacement run (`disp.end_index`) has completed — i.e. on the **return
move**, not the impulsive leg that created the block.

### State machine

```
ACTIVE -> PARTIALLY_MITIGATED -> FULLY_MITIGATED -> INVALIDATED -> ARCHIVED
                                        |
                                        +-> ARCHIVED (if no invalidation)
```

- **ACTIVE**: created; no candle since the displacement run ended has
  overlapped the zone `[low, high]`.
- **PARTIALLY_MITIGATED**: a later candle's range overlapped the zone, but
  no candle has yet CLOSED beyond its far boundary.
- **FULLY_MITIGATED**: a later candle's CLOSE moved beyond the zone's far
  boundary (bullish OB: close < low; bearish OB: close > high). The zone
  has been "used."
- **INVALIDATED**: stricter than FULLY_MITIGATED — within
  `invalidation_lookahead` candles after full mitigation, an *opposing*
  BOS/CHoCH structure event (from Task 1) confirms structure actually
  reversed against the OB's own direction. Without that confirming
  structure event, the OB stays FULLY_MITIGATED (it was tapped, but
  structure didn't necessarily flip).
- **ARCHIVED**: bookkeeping-only terminal state entered once
  `archive_after_candles` have elapsed since FULLY_MITIGATED/INVALIDATED.
  The row is **never deleted** — only `current_state` changes.

### Quality score (deterministic, rule-based, 0-1)

```
quality_score = 0.30 * freshness_score
              + 0.30 * displacement_score   (displacement total_range / (ATR*3), capped at 1)
              + 0.20 * body_score            (1 - wick_ratio)
              + 0.20 * age_score             (1 - age_in_candles / archive_after_candles, floored at 0)
```

`freshness_score` = 1.0 ACTIVE, 0.5 PARTIALLY_MITIGATED, 0.0 otherwise.
No AI, no randomness — same inputs always produce the same score.

---

## 3. Fair Value Gap Engine (`fvg.py`)

Classic 3-candle imbalance: bullish FVG at candle `i` when
`high[i-1] < low[i+1]`, zone `= [high[i-1], low[i+1]]`; bearish FVG when
`low[i-1] > high[i+1]`, zone `= [high[i+1], low[i-1]]`. `creation_timestamp`
is the CLOSE time of candle `i+1` (the confirming candle), since the gap
cannot be verified until that candle's low/high are final.

- `filled_percentage`: how deep subsequent candles have traded back into
  the zone from the counter-trend side, 0-100.
- `consequent_encroachment`: the 50% level of the zone; `ce_reached`
  records whether price has traded to at least that level.
- **PARTIALLY_FILLED**: `0 < filled_percentage < 100`.
- **FULLY_MITIGATED**: `filled_percentage >= 100` (the zone has been
  completely traded through).
- **EXPIRED**: `age >= expire_after_candles` with zero interaction
  (`filled_percentage == 0`) — bookkeeping only, not deleted.

---

## 4. Liquidity Engine (`liquidity.py`)

Built directly on top of `src.structure.swings.detect_swings` — **no
duplicated swing-detection logic**.

- Every confirmed swing high is **buy-side liquidity**; every confirmed
  swing low is **sell-side liquidity**.
- Swing highs (lows) whose prices cluster within
  `equal_level_tolerance` (relative) of each other merge into a single
  **Equal Highs** (**Equal Lows**) level, `number_of_touches` = cluster
  size. `strength` = "strong" if `number_of_touches >= min_touches_for_strength`.
- **External** liquidity: the level's price equals the running extreme
  (highest confirmed swing-high / lowest confirmed swing-low) seen up to
  that swing — i.e. it sits beyond the current dealing range. Otherwise
  **internal**.
- **Sweep** (unlike BOS/CHoCH, this is WICK-based, not close-based): a
  later candle's high/low trades beyond the level, but that SAME candle's
  close comes back on the origin side — the classic stop-hunt/liquidity
  grab pattern.

### State machine
```
ACTIVE -> SWEPT -> ARCHIVED
```
`ACTIVE` until a qualifying sweep candle occurs; `SWEPT` from that point;
`ARCHIVED` bookkeeping state after `archive_after_candles` have elapsed
since the sweep. Never deleted.

---

## 5. Session Engine (`sessions.py`)

Sessions are defined by **local civil time** windows in a named IANA
timezone (`config.settings.SESSION_WINDOWS_LOCAL`: Sydney, Tokyo/"Asian",
London, New York). For each calendar day in that session's own timezone,
the start/end are built as tz-aware local timestamps and converted to UTC.

**DST correctness**: because the window is anchored to local civil time
(e.g. "08:00 London time") and converted per calendar day, the resulting
UTC boundaries automatically shift by an hour across DST transitions —
verified in `tests/test_sessions.py` (London session starts at 08:00 UTC
in January, 07:00 UTC in July).

Output: one row per `(session_name, local_date)` with `open/high/low/close`
and `start_utc`/`end_utc`. "Asian High/Low" = the Tokyo session's high/low
in this v0.1 (documented simplification — Sydney is tracked separately and
could be merged into a combined Asian range in a later task).

---

## 6. Reference Level Engine (`reference_levels.py`)

Reuses `src.data.resample.resample_ohlc` for daily/weekly aggregation — no
duplicated OHLC logic.

- **PDH/PDL**: previous calendar day's high/low. `available_from` is set to
  the START of the next day — a day's high/low is not final until the day
  itself has ended, so it must never be usable during that same day.
- **PWH/PWL**: same logic at the week boundary (`W-SUN` weekly bars).
- **Weekend Gap** (`compute_weekend_gaps`): detected as a time jump of at
  least `min_gap_hours` (default 20h) where the earlier candle falls on a
  Friday. Records Friday close, Sunday/Monday reopen open, `gap_size`,
  `gap_pct`, `gap_direction`, and `gap_filled_pct` (how far price has
  traded back toward the Friday close). States: `OPEN` (0% filled),
  `PARTIALLY_FILLED`, `FILLED` (>=100%).

---

## 7. Engulfing Engine (`engulfing.py`)

Bullish engulfing at `i+1`: candle `i` bearish, candle `i+1` bullish, and
`i+1`'s body fully contains `i`'s body. Bearish engulfing is the mirror
image. Strength is a deterministic ratio of engulfing body size to
engulfed body size:

```
body_ratio >= strong_body_ratio (default 1.5)  -> STRONG
body_ratio >= normal_body_ratio (default 1.0)  -> NORMAL
otherwise                                       -> WEAK
```

`displacement_backed` reuses the Displacement Engine's per-candle ATR/
avg-body conditions on the engulfing candle (no duplicated logic); if
true, WEAK is never assigned (bumped to at least NORMAL/STRONG based on
the ratio).

---

## 8. Confluence Engine (`confluence.py`)

**Read-only.** Makes no trading decisions. `build_confluence_snapshot(df,
as_of_index, symbol, timeframe)` recomputes every engine above on the
history truncated to `as_of_index` and returns one dict of independently
queryable flags: `structure_state`, `last_structure_event_type/direction`,
active/fresh Order Block counts by direction, active FVG counts by
direction, `pdh_swept`, `pdl_swept`, `asian_low_swept`, `asian_high_swept`,
`strong_engulfing_recent`, `open_weekend_gap`, active liquidity level
count.

**Performance note**: each snapshot is O(n) (it recomputes history up to
`as_of_index`), so building one per candle over millions of rows is
O(n^2). Use `generate_confluence_dataset(df, ..., stride=N)` for research
sampling, or call snapshots only at specific decision points in a
backtest loop. A future task should replace this with incremental/
interval-indexed state if a dense per-candle confluence timeline becomes
necessary.

---

## Object lifecycle summary

| Object | States |
|---|---|
| Order Block | ACTIVE → PARTIALLY_MITIGATED → FULLY_MITIGATED → INVALIDATED → ARCHIVED |
| FVG | ACTIVE → PARTIALLY_FILLED → FULLY_MITIGATED (or → EXPIRED) |
| Liquidity Level | ACTIVE → SWEPT → ARCHIVED |
| Weekend Gap | OPEN → PARTIALLY_FILLED → FILLED |

No object is ever deleted from its dataset; only `current_state`/
`state`/`active_status` changes, so full research history is preserved.

---

## Configuration reference (`config/settings.py`)

`DisplacementConfig`, `OrderBlockConfig`, `FVGConfig`, `LiquidityConfig`,
`SessionConfig`, `ReferenceLevelConfig`, `EngulfingConfig` — see the
dataclass docstrings/fields for every tunable parameter and its default.

## Known limitations (v0.1)

- Order Block quality scoring omits "Trend Alignment" and "Liquidity
  Context" factors called out in the original spec — those require
  cross-engine composition (structure state + liquidity proximity at
  creation time) that is left for a future task once strategies define
  how they want to weight them.
- Asian session = Tokyo window only; Sydney is tracked separately.
- The Confluence Engine's O(n) per-snapshot cost is a known scaling
  limitation for dense per-candle timelines (see section 8).
- Liquidity "internal vs external" classification uses a single running
  extreme rather than a proper multi-scale dealing-range model.

## Example datasets

Run `python scripts/generate_feature_datasets.py --input data/raw/EURUSD_M1_synthetic.csv --symbol EURUSD --timeframe 15min` to (re)generate
`data/processed/{order_blocks,fvgs,liquidity,sessions,reference_levels,weekend_gaps,engulfing,confluence}.parquet`.

## Visual validation

```bash
python scripts/validate_structure.py --input data/raw/EURUSD_M1.csv \
    --symbol EURUSD --timeframe 15min --source-tz UTC --left 2 --right 2 \
    --out reports/eurusd_validation.html
```

Every category (swings, BOS/CHoCH, Order Blocks, FVGs, liquidity, PDH/PDL/
PWH/PWL, session highs/lows, weekend gaps) is a separate Plotly trace or
trace group — click any legend entry to toggle it on/off.
