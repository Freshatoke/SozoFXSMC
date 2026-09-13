# Video SMC Strategy Specification — "Revealing my Full SMC Trading Strategy (Live Trade Walkthrough)"

## Source

Based on a real, user-supplied verbatim transcript (with timestamps) of the video, not a paraphrase — direct video/caption access was attempted and failed the same way it did for Task 11.5 (YouTube's caption endpoint returns HTTP 200 with an empty body for non-session-bound requests; the video description contains no strategy content, only promotional links). Every rule below is quoted or closely paraphrased from that transcript. Anything the transcript did not make explicit is marked **UNKNOWN** — never invented.

## Phase 1 — Extracted rules

### 1. Bias for the day (HTF direction)

```
RULE:        Daily/session directional bias
INPUT:       Market structure (swing highs/lows), volume profile, buyer/
             seller interaction reading, "candle range theory"
TIMEFRAME:   Primarily 15-minute ("I use the 15-minute time frame to
             determine my bias"); also glances at 4-hour per his on-screen
             indicator panel (UNKNOWN exact weighting between the two)
CONDITION:   Market structure trending (higher-highs/higher-lows =
             bullish, or the reverse); volume profile showing rising
             transacted volume at higher price levels; discretionary
             reading of "buyers dominating" price action; correlated
             instrument (DXY) confirming the opposite bias for USD pairs
ENTRY:       n/a (this step only sets direction, not a trade trigger)
STOP:        n/a
TARGET:      n/a
INVALIDATION: A "change of character" (CHoCH) against the established
             structure invalidates the bias
```

**Critical caveat**: swing-high/low identification itself is explicitly a proprietary, undisclosed algorithm. Direct quote: *"I decided to do a massive test using all different types of parameters. Average true range, Fibonacci retracements, specific pip retracements, specific candlesticks, and I eventually came up with the best way... I just took my rules, put them into an indicator."* The exact rule is never given — it is a black box, even to the presenter's own past self before he built the indicator. **This is the single largest UNKNOWN in the entire strategy**: every downstream step (location, confirmation, entry) depends on "market structure," and market structure's exact computation is proprietary and non-reproducible from this video.

Volume profile, buyer/seller interaction narrative, and "candle range theory" are **UNKNOWN in mechanical detail** — volume profile is described qualitatively ("more volume at a level means more transactions") but no numeric threshold is given for what counts as a significant node; buyer/seller interaction is pure discretionary narrative ("sellers try, buyers come out on top") with no quantifiable rule; "candle range theory" is named once and never defined at all in this transcript.

### 2. Location of bias execution

```
RULE:        Where to concentrate attention for an entry
INPUT:       Fixed-range Volume Profile (swing low to swing high),
             specifically the Value Area High / high-volume nodes
TIMEFRAME:   Same swing range as the HTF bias
CONDITION:   A price zone where volume profile shows a "battle" was won
             by the side matching the bias (e.g., for a bullish bias, a
             zone where buyers previously absorbed selling pressure and
             price reversed upward)
ENTRY:       n/a (this only defines a zone to watch, not a trigger)
STOP:        n/a
TARGET:      n/a
INVALIDATION: UNKNOWN (not addressed)
```

Explicitly discretionary: the presenter states there can be multiple candidate locations ("I might have location one, location two, location three, location four") and explicitly does NOT take all of them ("what do I do? I just put four trades? ... then my bias becomes my edge. So I don't think that's a great idea either") — but never states the actual rule for choosing among candidates. **This selection step is undisclosed discretion**, not a rule.

### 3. Confirmation

```
RULE:        Confirm that the location is valid RIGHT NOW
INPUT:       Multi-timeframe market structure (15-minute = "general
             drift," 1-minute = "immediate drift"), volume, buyer/seller
             dynamics at the price level
TIMEFRAME:   1-minute (entry timeframe) vs. 15-minute (bias timeframe)
CONDITION:   Price reaches the location; the 1-minute timeframe's
             immediate structure REALIGNS with the 15-minute bias
             direction (a "change of character" on the 1-minute back
             toward the HTF bias) -- direct quote: "now we have the
             shortterm aligned with the long term. And that is your
             signal to go."
ENTRY:       See Step 4 below (a distinct sub-step)
STOP:        n/a
TARGET:      n/a
INVALIDATION: If the 1-minute structure does NOT realign (keeps moving
             against the HTF bias), no confirmation occurs and the setup
             is skipped ("we don't want to just sell and we don't want
             to just trust that this level will hold")
```

### 4. Entry trigger

```
RULE:        Precise entry price
INPUT:       Volume (highest-volume sub-node within the immediate
             "battle range"), an intraday Fair Value Gap / imbalance
TIMEFRAME:   1-minute
CONDITION:   Price fills into the identified high-volume node AND an
             intraday gap/FVG at the same zone -- direct quote: "I placed
             my long position also in the gap... most intraday gaps get
             filled"
ENTRY:       Limit/resting order inside the FVG/high-volume zone
STOP:        "Just at the low of this level" -- i.e., beyond the low of
             the confirmed location/battle-range zone
TARGET:      No fixed target stated -- see Trade Management below
INVALIDATION: UNKNOWN exact invalidation (e.g., max wait time for the
             fill) -- not addressed
```

### 5. Trade management (the strategy's most concretely detailed, and most distinctive, component)

```
RULE:        Dynamic scaling and stop adjustment as the trade runs
INPUT:       Realized R-multiple, renewed buyer/seller "battle" outcomes
             observed on lower timeframes as price extends
TIMEFRAME:   Continuous, monitored candle-by-candle
CONDITION:   After each fresh higher-low (bullish trade) is confirmed by
             a renewed win for the trade's direction in a seller-vs-buyer
             "battle," take a partial profit AND move the stop up to lock
             in gains
ENTRY:       n/a (management only)
STOP:        Trailed to just below each newly-confirmed swing/higher-low
             as the trade extends (STRUCTURE-based trailing, not a fixed
             distance or ATR multiple)
TARGET:      No single fixed target -- partial exits observed in this
             trade at approximately 2.5R, 3R (stop moved to lock 3R),
             4.3R, 6R, 7-8R, 8.5R, 9R, 10R, 11.3R, with the final runner
             closed at 13R when the trailing stop was finally hit.
             EXACT partial-exit percentages per level are UNKNOWN (never
             stated numerically) -- only the R-multiples at which SOME
             portion was closed are given, from the presenter's own
             narration of that specific trade's outcome
INVALIDATION: Trailing stop hit closes the remaining position
```

Direct quote on why this matters: *"This logically could have been a... 1 to 2. But because of my... dynamic take profits and trailing and managing and partialing and trailing my stops, this ends up turning into like a 1 to 8, which is 4x... That's 4x just from management."* This is the single clearest, most quantifiable claim in the video, and the component this specification treats as most worth testing (see `docs/VIDEO_SMC_VS_SOZOFX.md`).

### 6. Sessions

```
RULE:        Session context
INPUT:       Custom indicator's session boxes (Asia = blue, London =
             green, New York = orange)
CONDITION:   "I don't trade the Asia session, but I do use it for some
             important things" -- the "important things" are UNKNOWN,
             never specified. The example trade's entry occurs near a
             session open on the chart, but WHICH session (London vs.
             New York) is never stated explicitly in this transcript.
```

### 7. Symbol restriction

```
RULE:        Instrument selection
CONDITION:   "I trade primarily EURUSD." The US Dollar Index (DXY) is
             monitored as a correlated CONFIRMATION instrument (EURUSD
             is ~60-70% of the DXY basket weight, so they move
             inversely most of the time) -- DXY itself is NOT traded.
```

### 8. Day-of-week restriction

UNKNOWN -- not mentioned anywhere in this transcript.

### 9. Time-of-day restriction

UNKNOWN beyond "no Asia session trading" -- no specific entry window (e.g., "only the first hour of London") is stated as a hard rule.

## Phase 2 — Formal specification: mandatory vs. optional vs. discretion

### Mandatory conditions (stated as always required)
1. An established HTF market structure direction (bias)
2. A location where price previously reversed (volume-profile-based, per the video)
3. LTF (1-minute) structure realignment with the HTF bias at that location
4. A stop-loss below/above the location's extreme
5. No trading during the Asian session

### Optional / contextual conditions
1. DXY correlation check (used as corroborating evidence, not a hard gate — the presenter says his bias can be right even against the DXY reading in some cases)
2. Candle range theory (mentioned, never defined — cannot be classified as mandatory or optional since its role is unknown)
3. Specific session (London vs. New York) for entry timing

### Trader discretion (explicitly self-identified in the transcript)
1. **Swing-high/low identification itself** — "90% of the time yes [follow structure], but you have to becoming a better trader is understanding what's the 10% of the time where we say no" — direct admission of a 10% discretionary override rate on the MOST foundational input to the entire strategy.
2. **Which candidate location to trade** when multiple qualify ("location one, location two, location three, location four").
3. **Buyer/seller "battle" interpretation** — reading whether a level is being "defended" is explicitly framed as "a thesis... not betting on it," i.e., a subjective judgment, not a fired rule.
4. **Exact management trigger** for each partial-exit/trail step — "look at that, buyers stepped back in" is pattern recognition, not a numeric rule (e.g., not "trail after N candles" or "trail to the Nth swing low").

### Reproducibility verdict for Phase 2's own stated bar

The task requires the specification be *"precise enough that two independent developers could implement the same strategy and produce the same signals."* **This bar is not met for the bias/structure and location-selection layers** — the swing-detection algorithm is proprietary and undisclosed, and location selection among multiple candidates and the "battle" narrative are explicit discretion. It **is met, reasonably well**, for a specific, honestly-labeled *substitution*: using this codebase's own existing deterministic swing/structure/Order Block/FVG engine as the closest available proxy for "market structure" and "location," entering at an FVG, stopping beyond the zone extreme, and testing a staged partial-exit + structure-trailing management scheme. This substitution — not a literal reproduction of the video — is what `src/research/robustness/s6_video_smc_signals.py` (S6) implements, and it is documented as a substitution throughout, not represented as "the video's exact strategy."
