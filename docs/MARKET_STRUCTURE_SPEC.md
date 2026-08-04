# Market Structure Specification (v0.1)

This document is the authoritative definition of swing detection and the
BOS/CHoCH state machine implemented in `src/structure/`. It exists so that
any structural event produced by the engine can be traced back to *why* it
was classified that way.

## 1. Swing definition (`src/structure/swings.py`)

Method: **fractal** (the only method implemented in v0.1; the architecture
allows adding others behind the same interface later).

Given configurable `left` and `right` (default `2`, `2`):

- Candle `i` is a **confirmed swing high** if `high[i]` is strictly greater
  than the high of each of the `left` candles before it AND strictly
  greater than the high of each of the `right` candles after it.
- Candle `i` is a **confirmed swing low** if `low[i]` is strictly less than
  the low of each of the `left` candles before it AND strictly less than
  the low of each of the `right` candles after it.

### Swing occurred vs. swing confirmed

This is the most important anti-look-ahead property of the system:

- `swing_timestamp` — the timestamp of candle `i` itself (when the swing
  *occurred* in the price series).
- `confirmed_timestamp` — the close time of candle `i + right` (when the
  swing became *knowable* to any process walking forward through time).

`confirmed_timestamp` is always strictly after `swing_timestamp` (for
`right >= 1`). Any consumer of swing data MUST filter on
`confirmed_timestamp <= current_time`, never on `swing_timestamp`. Filtering
on `swing_timestamp` would let a strategy "see" a swing before enough future
candles existed to prove it was actually a swing — this is exactly the kind
of look-ahead bias the project must avoid.

## 2. Market structure state

Three states:

- `UNKNOWN` — not enough confirmed structure exists yet to classify the
  trend. This is the state at the start of any series, and after a
  structural level is broken but no opposing/new level has yet formed
  (edge case, rare).
- `BULLISH` — the most recent structural break was a close above the
  active swing high.
- `BEARISH` — the most recent structural break was a close below the
  active swing low.

The engine never retroactively labels early history as bullish/bearish
before a real break has occurred — state starts at `UNKNOWN` and only
changes on an actual break event.

## 3. Active levels

At any point in time the engine tracks at most:

- one **active (unbroken) confirmed swing high** — the candidate reference
  level for the next bullish break, and
- one **active (unbroken) confirmed swing low** — the candidate reference
  level for the next bearish break.

When a new confirmed swing of a given type becomes available (its
`confirmed_timestamp` has been reached), it replaces the previous active
level of that type **only if it is a later swing** (higher `candle_index`).
This means only the most recent unbroken swing of each type is ever
"live" — older, still-unbroken swings are superseded rather than tracked
as multiple candidate levels. This is a deliberate v0.1 simplification
(see Limitations).

## 4. BOS vs. CHoCH — the exact rule

A **break** happens when a candle's `close` (never a wick/high/low alone)
moves beyond the active level:

| Active level broken | Prior state | Event | New state |
|---|---|---|---|
| swing high (close > level) | `UNKNOWN` or `BULLISH` | **BOS**, bullish | `BULLISH` |
| swing high (close > level) | `BEARISH` | **CHoCH**, bullish | `BULLISH` |
| swing low (close < level) | `UNKNOWN` or `BEARISH` | **BOS**, bearish | `BEARISH` |
| swing low (close < level) | `BULLISH` | **CHoCH**, bearish | `BEARISH` |

In words:
- **BOS** = a break that continues/confirms the current (or not-yet-formed)
  trend direction.
- **CHoCH** = a break that moves in the opposite direction of the current
  established trend — a potential change of character.

A wick beyond the level with the candle closing back on the other side
does **not** qualify, by construction (the engine compares `close`, not
`high`/`low`, when `require_close_beyond_level=True`, the default).

## 5. Duplicate-event prevention

The instant a level is broken it is retired (`broken_high_keys` /
`broken_low_keys` in `market_structure.py`) and the active level for that
side is cleared. It cannot fire again even if subsequent candles keep
closing beyond the same price — the engine only reports the *first* break
of a given confirmed swing.

## 6. Event schema

Each row in the structure-event dataset contains:

```
event_id, symbol, timeframe, event_type (BOS|CHoCH), direction (bullish|bearish),
broken_level, broken_swing_timestamp, confirmation_timestamp,
break_candle_timestamp, break_candle_close,
previous_structure_state, new_structure_state,
price, swing_reference, structure_before, structure_after, metadata
```

`metadata` carries the swing-detection parameters (`left`, `right`,
`method`) active when the event was produced, so results remain
reproducible and auditable.

## 7. Configurable parameters

- `SwingConfig.left`, `SwingConfig.right` — fractal confirmation window.
- `SwingConfig.method` — pluggable, only `"fractal"` implemented.
- `StructureConfig.require_close_beyond_level` — always `True` by default;
  exposed as a parameter so alternative (non-compliant) break definitions
  can be studied for research/comparison purposes only.

## 8. Known limitations (v0.1)

- Only the single most recent unbroken swing per side is tracked as
  "active." Older unbroken swings further back are not queued as
  secondary candidate levels. This matches common simple SMC
  implementations but is a simplification worth revisiting once S2/S3/S4
  are built, since some of those strategies care about multiple
  untouched liquidity levels simultaneously.
- The fractal method only detects local extremes; it does not yet
  incorporate volatility-adjusted or multi-scale swing definitions.
  The architecture (`SwingConfig.method`) allows adding these later.
- Order Blocks, FVGs, liquidity sweeps and the five strategy modules are
  explicitly out of scope for this task.

## 9. Worked example

Given `left=1, right=1` and candles (`open, high, low, close`):

```
i0: 0.95, 1.00, 0.90, 0.95
i1: 1.00, 1.20, 0.95, 1.05   <- swing high candidate (1.20)
i2: 1.05, 1.10, 1.00, 1.05   <- confirms i1's swing; confirmed_timestamp = close of i2
i3: 1.05, 1.25, 1.00, 1.25   <- closes at 1.25 > 1.20 -> BOS bullish, state UNKNOWN -> BULLISH
```

If a later swing low forms and a candle subsequently *closes* below it while
state is `BULLISH`, that produces a bearish **CHoCH** (reversal), not a BOS.
