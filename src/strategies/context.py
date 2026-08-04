"""
Shared Market Context.

Strategies MUST NEVER implement their own BOS/CHoCH, Order Block, FVG,
Liquidity, or Session detection -- they query this context, which computes
each of those exactly once (per timeframe, lazily and cached) using the
existing Task 1 (`src.structure`) and Task 2 (`src.features`) engines.
Running five strategies over the same context therefore does not
duplicate any detection work.

All computation here is batch/research-style (the same reference
implementations validated in Task 1/2's test suites), operating over a
fixed historical DataFrame -- this module does not touch the Task 2.5
incremental engine. A strategy's own signal-generation loop is what
prevents look-ahead (see each strategy module's docstring): it only reads
context data whose timestamps are <= the candle being evaluated.

PERFORMANCE NOTE (Task 3): this environment's pandas (3.x) defaults string
columns to a pyarrow-backed dtype, which makes repeated boolean-mask
filtering over a DataFrame extremely slow when called once per candle
inside a strategy's scan loop (profiled: >85% of total runtime in pyarrow
`compute.take`/`cast` for what should be simple point-in-time lookups).
The "as of timestamp" query helpers below therefore convert each
DataFrame to a plain Python list of dicts EXACTLY ONCE (cached) and do
all repeated point-in-time lookups as plain Python loops/comparisons
against that list -- no pandas/pyarrow machinery touched per call.

PERFORMANCE NOTE (Task 7.4): converting to a cached list was necessary
but not sufficient. Every strategy scans candles in increasing timestamp
order, calling these "as of" helpers once per scanned candle -- but the
original implementation re-scanned each cached list from index 0 on
EVERY call, even though the list only grows over the life of a context.
Profiling a 3-month real-data run showed `latest_choch_asof` and
`fresh_order_block_asof` alone accounted for ~22% of total wall time
(119s of 542s), with call counts in the tens of thousands against lists
that grow throughout the run -- an O(n) scan repeated O(calls) times is
effectively O(n^2) in the number of candles processed, which is exactly
the "worse than linear" scaling measured end to end (see
docs/PERFORMANCE_OPTIMIZATION.md).

The records in each cache are chronologically ordered by construction
(structure events, order blocks, and FVGs are all detected in a single
forward pass over ascending candle indices -- see
`src.structure.market_structure`, `src.features.order_blocks`). This
means `bisect.bisect_right(records, timestamp, key=...)` finds the exact
cutoff position in O(log n), and iterating BACKWARD from that cutoff
finds the same "most recent qualifying record" the original forward
scan-and-overwrite loop found, in O(k) where k is the (typically small)
number of records examined before a match -- never O(n). This is a pure
access-pattern change: every filtering rule is unchanged, so results are
identical to the pre-optimization implementation (verified via
`scripts/golden_snapshot.py` before/after comparison, not just by
argument).
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field

import pandas as pd

from config.settings import (
    SwingConfig, StructureConfig, DisplacementConfig, OrderBlockConfig,
    FVGConfig, LiquidityConfig, SessionConfig, EngulfingConfig,
)
from src.data.resample import resample_ohlc
from src.structure.swings import detect_swings
from src.structure.market_structure import detect_structure_events
from src.features.displacement import detect_displacement
from src.features.order_blocks import detect_order_blocks
from src.features.fvg import detect_fvgs
from src.features.liquidity import detect_liquidity_levels
from src.features.sessions import compute_sessions
from src.features.reference_levels import compute_reference_levels, compute_weekend_gaps
from src.features.engulfing import detect_engulfing


@dataclass
class MarketContext:
    symbol: str
    m1: pd.DataFrame
    swing_config: SwingConfig = field(default_factory=SwingConfig)
    structure_config: StructureConfig = field(default_factory=StructureConfig)
    displacement_config: DisplacementConfig = field(default_factory=DisplacementConfig)
    ob_config: OrderBlockConfig = field(default_factory=OrderBlockConfig)
    fvg_config: FVGConfig = field(default_factory=FVGConfig)
    liquidity_config: LiquidityConfig = field(default_factory=LiquidityConfig)
    session_config: SessionConfig = field(default_factory=SessionConfig)
    engulfing_config: EngulfingConfig = field(default_factory=EngulfingConfig)

    def __post_init__(self):
        self._candles_cache: dict[str, pd.DataFrame] = {"M1": self.m1}
        self._swings_cache: dict[str, pd.DataFrame] = {}
        self._structure_cache: dict[str, pd.DataFrame] = {}
        self._structure_records_cache: dict[str, list] = {}
        self._ob_cache: dict[str, pd.DataFrame] = {}
        self._ob_records_cache: dict[str, list] = {}
        self._fvg_cache: dict[str, pd.DataFrame] = {}
        self._liquidity_cache: dict[str, pd.DataFrame] = {}
        self._displacement_cache: dict[str, pd.DataFrame] = {}
        self._sessions: pd.DataFrame | None = None
        self._session_records: list | None = None
        self._session_records_by_name_cache: dict[str, list] | None = None
        self._reference_levels: pd.DataFrame | None = None
        self._weekend_gaps: pd.DataFrame | None = None
        self._engulfing: pd.DataFrame | None = None

    # --- candles ---------------------------------------------------------

    def candles(self, timeframe: str) -> pd.DataFrame:
        if timeframe not in self._candles_cache:
            self._candles_cache[timeframe] = resample_ohlc(self.m1, _TF_TO_PANDAS[timeframe])
        return self._candles_cache[timeframe]

    # --- structure ---------------------------------------------------------

    def swings(self, timeframe: str) -> pd.DataFrame:
        if timeframe not in self._swings_cache:
            self._swings_cache[timeframe] = detect_swings(
                self.candles(timeframe), config=self.swing_config, timeframe_label=timeframe,
            )
        return self._swings_cache[timeframe]

    def structure_events(self, timeframe: str) -> pd.DataFrame:
        if timeframe not in self._structure_cache:
            self._structure_cache[timeframe] = detect_structure_events(
                self.candles(timeframe), self.swings(timeframe),
                symbol=self.symbol, timeframe=timeframe, config=self.structure_config,
            )
        return self._structure_cache[timeframe]

    def _structure_records(self, timeframe: str) -> list:
        """Plain-Python cache of structure_events, converted exactly once.
        See module docstring's performance note for why."""
        if timeframe not in self._structure_records_cache:
            events = self.structure_events(timeframe)
            self._structure_records_cache[timeframe] = events.to_dict("records")
        return self._structure_records_cache[timeframe]

    def structure_state_asof(self, timeframe: str, timestamp) -> str:
        """The market structure state as of (<=) `timestamp` on `timeframe`.

        O(log n) via bisect -- see the module's Task 7.4 performance note.
        `records` is sorted ascending by `break_candle_timestamp` by
        construction, so the cutoff index found by `bisect_right` is the
        same "last record with break_candle_timestamp <= timestamp" the
        original forward scan-and-overwrite loop would have landed on.
        """
        records = self._structure_records(timeframe)
        idx = bisect.bisect_right(records, timestamp, key=lambda r: r["break_candle_timestamp"])
        return records[idx - 1]["new_structure_state"] if idx > 0 else "UNKNOWN"

    def latest_choch_asof(self, timeframe: str, timestamp, direction: str | None = None):
        """Returns the most recent CHoCH event at/before `timestamp` on
        `timeframe`, optionally filtered by direction, or None.

        O(log n + k): bisect to the cutoff, then scan backward -- the
        first CHoCH (matching `direction` if given) found walking
        backward from the cutoff is the same record the original forward
        scan-and-overwrite loop would have returned, since that loop kept
        overwriting `best` with each later match and the list is
        chronologically ordered.
        """
        records = self._structure_records(timeframe)
        idx = bisect.bisect_right(records, timestamp, key=lambda r: r["break_candle_timestamp"])
        for i in range(idx - 1, -1, -1):
            rec = records[i]
            if rec["event_type"] != "CHoCH":
                continue
            if direction and rec["direction"] != direction:
                continue
            return rec
        return None

    # --- SMC features ---------------------------------------------------------

    def displacement(self, timeframe: str) -> pd.DataFrame:
        if timeframe not in self._displacement_cache:
            self._displacement_cache[timeframe] = detect_displacement(
                self.candles(timeframe), config=self.displacement_config,
            )
        return self._displacement_cache[timeframe]

    def order_blocks(self, timeframe: str) -> pd.DataFrame:
        if timeframe not in self._ob_cache:
            obs, _ = detect_order_blocks(
                self.candles(timeframe), self.symbol, timeframe,
                config=self.ob_config, structure_events=self.structure_events(timeframe),
            )
            self._ob_cache[timeframe] = obs
        return self._ob_cache[timeframe]

    def _ob_records(self, timeframe: str) -> list:
        if timeframe not in self._ob_records_cache:
            records = self.order_blocks(timeframe).to_dict("records")
            # Task 7.4 PERFORMANCE NOTE: precompute each record's
            # touched/untouched null-ness once here instead of calling
            # pd.isna() on every record visited by fresh_order_block_asof's
            # backward scan -- profiling showed pd.isna() called millions
            # of times across a multi-month run. first_touch_timestamp is a
            # fixed fact once this dataset is built (see the docstring on
            # fresh_order_block_asof), so caching its null-ness is safe.
            for rec in records:
                touch = rec["first_touch_timestamp"]
                rec["_touch_is_null"] = touch is None or pd.isna(touch)
            self._ob_records_cache[timeframe] = records
        return self._ob_records_cache[timeframe]

    def fvgs(self, timeframe: str) -> pd.DataFrame:
        if timeframe not in self._fvg_cache:
            self._fvg_cache[timeframe] = detect_fvgs(self.candles(timeframe), self.symbol, timeframe, config=self.fvg_config)
        return self._fvg_cache[timeframe]

    def liquidity(self, timeframe: str) -> pd.DataFrame:
        if timeframe not in self._liquidity_cache:
            self._liquidity_cache[timeframe] = detect_liquidity_levels(
                self.candles(timeframe), self.symbol, timeframe, config=self.liquidity_config,
            )
        return self._liquidity_cache[timeframe]

    @property
    def sessions(self) -> pd.DataFrame:
        if self._sessions is None:
            self._sessions = compute_sessions(self.m1, config=self.session_config)
        return self._sessions

    def _session_records_list(self) -> list:
        if self._session_records is None:
            self._session_records = self.sessions.to_dict("records")
        return self._session_records

    def _session_records_by_name(self) -> dict:
        # Task 7.4 PERFORMANCE NOTE: session_active_asof originally did a
        # full linear scan of ALL session records (every name interleaved)
        # from index 0 on every call -- an O(n) scan whose call count also
        # grows with the number of candles processed, i.e. the same O(n^2)
        # class of bug already fixed in latest_choch_asof/fresh_order_block_asof.
        # Sessions of a single name never overlap (each day's session ends
        # before the next day's of the same name begins) and are emitted in
        # ascending start_utc order per name by src.features.sessions, so
        # grouping by name once and bisecting within that name's own list
        # finds the (at most one) candidate session in O(log n).
        if self._session_records_by_name_cache is None:
            by_name: dict[str, list] = {}
            for rec in self._session_records_list():
                by_name.setdefault(rec["session_name"], []).append(rec)
            self._session_records_by_name_cache = by_name
        return self._session_records_by_name_cache

    @property
    def reference_levels(self) -> pd.DataFrame:
        if self._reference_levels is None:
            self._reference_levels = compute_reference_levels(self.m1)
        return self._reference_levels

    @property
    def weekend_gaps(self) -> pd.DataFrame:
        if self._weekend_gaps is None:
            self._weekend_gaps = compute_weekend_gaps(self.m1)
        return self._weekend_gaps

    @property
    def engulfing(self) -> pd.DataFrame:
        if self._engulfing is None:
            self._engulfing = detect_engulfing(self.m1, config=self.engulfing_config)
        return self._engulfing

    # --- convenience queries used by every strategy ---------------------------------------------------------

    def fresh_order_block_asof(self, timeframe: str, direction: str, timestamp, price_near: float | None = None, tolerance: float = 0.01):
        """Returns the most recent FRESH (never touched, AS OF `timestamp`)
        OB of `direction` created at/before `timestamp`, optionally
        filtered to be within `tolerance` (relative) of `price_near`.

        IMPORTANT: `order_blocks(timeframe)` is computed once over the
        FULL history, so its `current_state`/`freshness_status` columns
        describe each OB's FINAL outcome (as of the end of the dataset),
        not its state at `timestamp` -- filtering on those directly would
        be look-ahead bias (an OB that gets touched next week would wrongly
        be excluded from "fresh" evaluations made today). `first_touch_timestamp`
        is itself a fixed, immutable fact about exactly when (if ever) the
        zone was first touched; comparing it against `timestamp` correctly
        reconstructs point-in-time freshness without needing any
        recomputation, since "was it touched by `timestamp`" only depends
        on candles up to `timestamp` regardless of what the dataset knows
        about later candles.

        O(log n + k), same bisect-then-scan-backward pattern as
        `latest_choch_asof` -- `records` is sorted ascending by
        `creation_timestamp` by construction (Order Blocks are created in
        a single forward pass over candles), and since the original loop
        always overwrote `best` with the later-created qualifying record
        (creation_timestamp strictly increases through the list), the
        first qualifying record found walking backward from the
        `timestamp` cutoff is identical to what the original loop returned.
        """
        records = self._ob_records(timeframe)
        idx = bisect.bisect_right(records, timestamp, key=lambda r: r["creation_timestamp"])
        for i in range(idx - 1, -1, -1):
            rec = records[i]
            if rec["direction"] != direction:
                continue
            touch = rec["first_touch_timestamp"]
            if not rec["_touch_is_null"] and touch <= timestamp:
                continue
            if price_near is not None and not (rec["low"] <= price_near * (1 + tolerance) and rec["high"] >= price_near * (1 - tolerance)):
                continue
            return rec
        return None

    def active_fvg_asof(self, timeframe: str, direction: str, timestamp):
        """Returns the most recent FVG of `direction` that is still active
        (not fully mitigated) AS OF `timestamp`.

        Unlike Order Blocks, `src.features.fvg` does not record a
        "full-mitigation timestamp" -- only the FINAL `filled_percentage`/
        `active_status` over the whole dataset. Using those directly would
        be the same look-ahead bug described above. Since this method is
        only ever invoked when a strategy's `require_fvg=True` (an opt-in,
        off by default), we pay for correctness by recomputing FVGs fresh
        with the batch engine's own `as_of_index` cutoff (the same
        mechanism Task 2's confluence engine uses) rather than trusting
        the full-history summary.
        """
        candles = self.candles(timeframe)
        eligible = candles.index[candles["timestamp"] <= timestamp]
        if len(eligible) == 0:
            return None
        as_of_index = int(eligible.max())
        fvgs_asof = detect_fvgs(candles, self.symbol, timeframe, config=self.fvg_config, as_of_index=as_of_index)
        best = None
        for rec in fvgs_asof.to_dict("records"):
            if rec["direction"] != direction or rec["creation_timestamp"] > timestamp:
                continue
            if rec["active_status"] not in ("ACTIVE", "PARTIALLY_FILLED"):
                continue
            if best is None or rec["creation_timestamp"] > best["creation_timestamp"]:
                best = rec
        return best

    def session_active_asof(self, session_name: str, timestamp) -> bool:
        records = self._session_records_by_name().get(session_name, [])
        idx = bisect.bisect_right(records, timestamp, key=lambda r: r["start_utc"])
        if idx == 0:
            return False
        rec = records[idx - 1]
        return rec["start_utc"] <= timestamp < rec["end_utc"]


_TF_TO_PANDAS = {"M1": "1min", "M5": "5min", "M15": "15min"}
