# Video Methodology Extraction — "Claude Tested Over 25,000 ICT Strategies (Here's What Works)"

## Source and confidence caveat (read first)

**This document is NOT built from a verified transcript of the video.** Direct access was attempted and failed: YouTube's auto-caption endpoint returned the correct signed URL from the page source, but the response body was empty (HTTP 200, `Content-Length: 0`) — YouTube's caption delivery now appears to require a session/token binding that a plain authenticated-free fetch cannot satisfy. No secondary transcript, write-up, or discussion of this specific video (published ~4 weeks prior, per web search) could be found. Only the video's public description and chapter markers were legitimately retrievable:

```
00:00 Testing Overview
04:30 Methodology
07:35 What Survived
14:35 Final Results
18:03 Claude Prompts
```

Given this, the user supplied a detailed written reconstruction of the video's methodology and findings, explicitly caveated by the user as **"not verbatim… a detailed description… to guide your own independent research."** Everything in this document is drawn from that user-supplied reconstruction, not from the video directly. Per this task's own rule ("do not invent details not supported by the video… mark as UNKNOWN"), anything the reconstruction did not specify is marked **UNKNOWN** below rather than filled in with plausible-sounding detail.

## Phase 1 answers

| Question | Answer | Confidence |
|---|---|---|
| What was tested? | ICT "Silver Bullet" concept (liquidity → sweep → displacement → FVG → retrace → entry → target), converted into parameterized, machine-testable rules, on NQ and ES futures. | From reconstruction |
| What does "25,000 strategies" mean? | ~25,000 sampled configurations from a much larger combinatorial space (~258 million possible combinations) — a sampled search, not an exhaustive one. | From reconstruction |
| How were combinations generated? | UNKNOWN — reconstruction states 25,000 were "sampled and tested" from 258M possible, but does not specify the sampling method (random sampling? grid with pruning? Latin hypercube? evolutionary search?). | UNKNOWN |
| Which ICT/SMC concepts were used? | Liquidity (previous/equal/session highs-lows), liquidity sweep, displacement, Fair Value Gap, (implicitly) market structure/retracement. Order Blocks and kill-zone windows are discussed but reported as NOT central to the surviving strategies. | From reconstruction |
| Which timeframes? | UNKNOWN — no specific timeframe(s) (M1, M5, M15 etc.) for signal detection are named in the reconstruction, only "16 years of 1-minute historical data" as the underlying dataset resolution. | UNKNOWN |
| Which entry conditions? | Tested as a parameter set: immediate entry after confirmation, limit at FVG, partial/full FVG retracement, entry after additional confirmation, entry after displacement, entry after sweep+FVG. No single entry rule is asserted as "the" winning one. | From reconstruction |
| Which confirmation conditions? | Liquidity sweep + displacement + FVG formation, in the Silver Bullet sequence; exact confirmation logic (e.g., CHoCH requirement) is UNKNOWN. | Partial |
| Which stop-loss rules? | Tested as a parameter set: beyond sweep, beyond swing high/low, beyond FVG, fixed %, ATR-based, fixed distance, structural. No single winning rule identified. | From reconstruction |
| Which take-profit rules? | Tested as a parameter set: fixed R multiples (1R–3R+), opposing liquidity, previous high/low, session high/low, dynamic structural target, trailing stop. No single winning rule identified. | From reconstruction |
| Which risk/reward assumptions? | Best-performing configurations reportedly clustered around ~52% win rate with a ~2:1 reward:risk framing used as the illustrative example — NOT necessarily the exact parameters of the actual best strategy. | From reconstruction, illustrative only |
| Which session filters? | UNKNOWN for the original NQ/ES research (time-of-day/kill-zone windows were tested as a variable, not assumed). For the Forex transfer, session candidates listed are Asian/London/New York/London-NY overlap/entire day. | Partial |
| Which market filters? | UNKNOWN — no volatility regime, trend/range, or news filters are mentioned as tested in the original research. | UNKNOWN |
| Which liquidity conditions? | Previous highs/lows, equal highs/lows (with a tolerance parameter), session highs/lows, generic swing points — all tested as configurable definitions, not fixed. | From reconstruction |
| Which Order Block/FVG conditions? | FVG: min/max size, retracement depth required, candles-until-invalid, must-remain-unfilled — all tested as parameters. Order Blocks: mentioned as a concept but NOT reported among the features that "survived" as important (liquidity sweeps were the standout, not OBs). | From reconstruction |
| Which market structure conditions? | UNKNOWN in detail — BOS/CHoCH are not explicitly named as tested variables in the reconstruction (the Silver Bullet sequence implies a structural reversal, but the exact structural confirmation logic used is not specified). | UNKNOWN |
| How were strategies ranked? | Explicitly stated to NOT be pure historical-profit ranking. Multiple metrics implied: expectancy, robustness, drawdown, risk-adjusted return — but the exact ranking formula/weighting is UNKNOWN. | Partial |
| What performance metrics were used? | Net profit, profit factor, win rate, average R, expectancy, max drawdown, Sharpe, Sortino, trade count, recovery factor, longest losing streak, monthly/yearly consistency — listed as metrics the creator says SHOULD be used; unclear which were actually computed and reported on-screen for the winning strategies vs. which are the video's general prescription. | Partial |
| How was overfitting addressed? | Filtering for "robustness" (out-of-sample performance, randomized trade sequences, Monte Carlo, parameter sensitivity, cross-market/period stability) is described as the INTENDED approach; only ~1.7% of 25,000 configurations reportedly survived. Exact pass/fail thresholds for each robustness test are UNKNOWN. | Partial |
| How was look-ahead bias avoided? | UNKNOWN — not addressed in the reconstruction at all. No statement about signal timing, entry-on-next-candle discipline, or data leakage prevention. | UNKNOWN |
| Were spreads, commissions, slippage included? | Yes, stated explicitly: "Trading costs were incorporated, including commissions and slippage." Exact cost assumptions (spread size, commission per contract, slippage model) are UNKNOWN. | Partial |
| Was walk-forward testing performed? | Described as part of the RECOMMENDED methodology ("perform walk-forward testing" in the "avoiding data mining" checklist) — not confirmed as something that was actually executed and reported on for the NQ/ES results specifically. | UNKNOWN whether actually executed vs. prescribed |
| Was out-of-sample testing performed? | Same as above — explicitly recommended/described as part of the robustness process, but the reconstruction does not confirm concrete out-of-sample results (e.g., "in-sample X% return, out-of-sample Y% return") were shown for the winning strategy. | UNKNOWN whether actually executed vs. prescribed |

## Phase 2 — Formal, machine-testable specification

Every component below is written in the INPUT / CONDITION / TIMEFRAME / ENTRY / STOP / TARGET / INVALIDATION / OUTPUT shape requested. Fields the reconstruction left unspecified are marked `UNKNOWN` — they are NOT filled with assumed defaults, since doing so would misrepresent them as coming from the video.

### Component: Liquidity Level

```
INPUT:        OHLC candle series, timeframe = UNKNOWN
CONDITION:    A swing point (previous high/low, equal high/low within a
              tolerance, session high/low) qualifies as a liquidity level.
              Tolerance for "equal" = a tested PARAMETER, not fixed.
TIMEFRAME:    UNKNOWN
ENTRY:        n/a (this is a level definition, not a trade trigger)
STOP:         n/a
TARGET:       n/a
INVALIDATION: Level is consumed/archived once swept (see Liquidity Sweep)
OUTPUT:       A price level + type (prev_high/prev_low/equal_high/equal_low/
              session_high/session_low) available to downstream components
```

### Component: Liquidity Sweep

```
INPUT:        A Liquidity Level, subsequent candles
CONDITION:    Price trades beyond the level by >= PENETRATION_PARAM,
              then closes back inside the prior range within
              <= RETURN_CANDLES_PARAM candles.
              PENETRATION_PARAM and RETURN_CANDLES_PARAM are explicitly
              tested as parameters, not fixed constants -- exact tested
              value ranges are UNKNOWN.
TIMEFRAME:    UNKNOWN
ENTRY:        n/a (precondition for downstream Silver Bullet sequence)
STOP:         n/a
TARGET:       n/a
INVALIDATION: If price does not close back inside within the candle
              budget, no sweep is recorded (or a "failed sweep" state --
              UNKNOWN which)
OUTPUT:       Sweep event: level, direction, penetration depth, candles-
              to-return
```

### Component: Displacement

```
INPUT:        Candle series following a Sweep event
CONDITION:    ONE OR MORE of: candle body size, candle range, ATR
              multiple, % move, consecutive directional candles, close
              position in range, speed of movement -- exceeds a tested
              threshold. Exact combination/weighting logic used for the
              "winning" definition(s) is UNKNOWN (multiple candidate
              measures were tested independently, per the reconstruction).
TIMEFRAME:    UNKNOWN
ENTRY:        n/a
STOP:         n/a
TARGET:       n/a
INVALIDATION: UNKNOWN
OUTPUT:       Displacement event: direction, magnitude, candle range
```

### Component: Fair Value Gap (FVG)

```
INPUT:        3-candle pattern following/during a Displacement event
CONDITION:    Gap size between MIN_SIZE_PARAM and MAX_SIZE_PARAM
              (both tested as parameters). Optionally required to form
              immediately after the sweep (tested as required/optional).
TIMEFRAME:    UNKNOWN
ENTRY:        n/a (precondition)
STOP:         n/a
TARGET:       n/a
INVALIDATION: Expires after CANDLES_UNTIL_INVALID_PARAM candles with no
              interaction; optionally invalidated if fully filled before
              entry (tested as required/optional "must remain unfilled")
OUTPUT:       FVG zone: high, low, formation candle, fill state
```

### Component: Entry

```
INPUT:        Sweep + Displacement + FVG (composition of required
              components is itself a tested variable -- e.g. FVG
              required vs optional)
CONDITION:    One of: immediate entry after confirmation / limit order
              at FVG boundary / partial FVG retracement (depth = tested
              parameter) / full FVG retracement / entry after additional
              confirmation (nature of "additional confirmation" =
              UNKNOWN) / entry after displacement / entry after sweep+FVG
TIMEFRAME:    UNKNOWN (entry-timeframe distinct from detection-timeframe
              is not addressed)
ENTRY:        Per the selected entry method above; exact trigger logic
              (limit vs. market, retracement %) = tested parameter set,
              not a single fixed rule
STOP:         See Stop-Loss component
TARGET:       See Take-Profit component
INVALIDATION: UNKNOWN (e.g., max candles to wait for entry trigger before
              abandoning the setup is not specified)
OUTPUT:       A trade record (direction, entry price, timestamp)
```

### Component: Stop-Loss

```
INPUT:        Trade entry, associated Sweep/FVG/structure levels
CONDITION:    One of: beyond the sweep extreme / beyond swing high-low /
              beyond FVG / fixed % / ATR-based / fixed distance /
              structural stop -- tested as a parameter set
TIMEFRAME:    n/a
ENTRY:        n/a
STOP:         Per selected method; exact buffer/multiple values = UNKNOWN
TARGET:       n/a
INVALIDATION: n/a
OUTPUT:       Stop price
```

### Component: Take-Profit

```
INPUT:        Trade entry, stop price (for R-multiple targets),
              liquidity/structure levels
CONDITION:    One of: fixed R (1R/1.5R/2R/2.5R/3R) / opposing liquidity /
              previous high-low / session high-low / dynamic structural
              target / trailing stop -- tested as a parameter set
TIMEFRAME:    n/a
ENTRY:        n/a
STOP:         n/a
TARGET:       Per selected method; exact winning method = UNKNOWN
              (reconstruction gives ~2:1 R:R only as an ILLUSTRATIVE
              example of what a 52%-win-rate strategy could look like,
              not a confirmed result)
INVALIDATION: n/a
OUTPUT:       Target price or exit rule
```

### Component: Time Window / Session Filter

```
INPUT:        Trade candidate timestamp
CONDITION:    Time-of-day membership in a tested window (ICT Silver
              Bullet kill zones vs. other windows, tested against each
              other rather than assumed superior)
TIMEFRAME:    n/a
ENTRY:        n/a (gating condition, not itself a trigger)
STOP:         n/a
TARGET:       n/a
INVALIDATION: n/a
OUTPUT:       Boolean pass/fail per candidate window
FINDING:      Reported conclusion: the specific ICT Silver Bullet windows
              did NOT show an extraordinary statistical advantage over
              other tested windows. Exact windows tested and their
              relative performance = UNKNOWN.
```

## Explicit UNKNOWNs (do not treat as resolved)

- Exact sampling/search algorithm used to select 25,000 of 258M combinations
- Exact timeframe(s) used for signal detection
- Exact structural (BOS/CHoCH) confirmation logic, if any, in the tested sequence
- Exact ranking formula/weights across the listed metrics
- Exact robustness-test pass/fail thresholds
- Whether look-ahead bias prevention was addressed at all
- Exact cost model (spread size, commission, slippage magnitude)
- Whether walk-forward / out-of-sample testing was actually executed and reported for the specific winning strategy, vs. only recommended as future practice
- The exact parameters of the single best-performing configuration (win rate ~52% and R:R ~2:1 are given only as an illustrative example, not confirmed as the actual winner's numbers)
