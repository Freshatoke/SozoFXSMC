"""
Task 11 Phase 3 — Incremental Market Context.

Bridges `src.engine.IncrementalEngine` (Task 2.5's streaming trackers --
swings, structure, Order Blocks, FVGs, liquidity, sessions, reference
levels; already cross-checked against the batch implementation in
tests/test_incremental_engine.py) to the `MarketContext`-shaped interface
`src.strategies.s3_liquidity_sweep`/`s4_pdh_pdl_sweep` already expect, so
those strategy modules run UNCHANGED against live data. This is the
"without rebuilding years of history" requirement: every new candle only
updates the incremental trackers' current state, never re-derives
anything from full history.

Three `IncrementalEngine` instances run in parallel (M1, M5, M15) --
S3/S4 need CHoCH confirmation on M5 and Order Blocks on M15 by default,
same as the batch pipeline. M5/M15 bars are built from the M1 stream by
`IncrementalResampler`, replicating `src.data.resample.resample_ohlc`'s
exact bucket convention (label='left', closed='left') so a live M5/M15
bar is identical to what batch resampling would have produced -- just
emitted the instant its bucket closes instead of after the fact.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from config.settings import (
    SwingConfig, StructureConfig, DisplacementConfig, OrderBlockConfig,
    FVGConfig, LiquidityConfig, SessionConfig, EngulfingConfig,
)
from src.engine.engine import IncrementalEngine
from src.engine.incremental import Candle
from src.engine.event_bus import EventBus


class IncrementalResampler:
    """Accumulates M1 candles into `target_timeframe` buckets, emitting a
    completed bar (as a `Candle`) the instant the NEXT bucket starts --
    matching resample_ohlc's label='left'/closed='left' convention with
    no look-ahead (a bucket is only ever emitted once its own bar_close_time
    has passed, i.e. once a later M1 candle proves it's finished)."""

    def __init__(self, target_timeframe: str):
        self.offset = pd.tseries.frequencies.to_offset(target_timeframe)
        self._bucket_start: Optional[pd.Timestamp] = None
        self._open = self._high = self._low = self._close = None
        self._index = 0

    def _bucket_for(self, ts: pd.Timestamp) -> pd.Timestamp:
        return ts.floor(self.offset)

    def ingest(self, candle: Candle) -> Optional[Candle]:
        """Returns a completed bar if this M1 candle closed one, else None."""
        bucket = self._bucket_for(candle.timestamp)
        completed = None
        if self._bucket_start is not None and bucket != self._bucket_start:
            completed = Candle(
                index=self._index, timestamp=self._bucket_start,
                open=self._open, high=self._high, low=self._low, close=self._close,
            )
            self._index += 1
            self._bucket_start = None

        if self._bucket_start is None:
            self._bucket_start = bucket
            self._open, self._high, self._low, self._close = candle.open, candle.high, candle.low, candle.close
        else:
            self._high = max(self._high, candle.high)
            self._low = min(self._low, candle.low)
            self._close = candle.close

        return completed


class LiveMarketContext:
    """Drop-in, MarketContext-shaped view over three parallel
    IncrementalEngine streams (M1/M5/M15). Implements exactly the
    subset of MarketContext's interface S3/S4 actually call:
    m1, symbol, liquidity(), fresh_order_block_asof(), latest_choch_asof(),
    active_fvg_asof(), session_active_asof(), structure_state_asof(),
    session_config, sessions, order_blocks(), displacement().

    "asof timestamp" queries always mean "as of the latest processed M1
    candle" here -- a live system has no future to look ahead into, so
    the incremental trackers' CURRENT active state IS the as-of-now
    answer, without any of the bisect/backward-scan machinery the batch
    MarketContext needs to reconstruct a historical point in time.
    """

    def __init__(self, symbol: str, session_config: SessionConfig = SessionConfig(),
                 swing_config: SwingConfig = SwingConfig(), structure_config: StructureConfig = StructureConfig(),
                 displacement_config: DisplacementConfig = DisplacementConfig(), ob_config: OrderBlockConfig = OrderBlockConfig(),
                 fvg_config: FVGConfig = FVGConfig(), liquidity_config: LiquidityConfig = LiquidityConfig(),
                 engulfing_config: EngulfingConfig = EngulfingConfig(), m1_history_len: int = 50_000):
        self.symbol = symbol
        self.session_config = session_config
        self._m1_rows: list = []
        self.m1_history_len = m1_history_len

        event_bus = EventBus()
        self.engines = {
            "M1": IncrementalEngine(symbol, "M1", pd.Timedelta(minutes=1), swing_config, structure_config,
                                     displacement_config, ob_config, fvg_config, liquidity_config, session_config, engulfing_config, event_bus),
            "M5": IncrementalEngine(symbol, "M5", pd.Timedelta(minutes=5), swing_config, structure_config,
                                     displacement_config, ob_config, fvg_config, liquidity_config, session_config, engulfing_config, event_bus),
            "M15": IncrementalEngine(symbol, "M15", pd.Timedelta(minutes=15), swing_config, structure_config,
                                      displacement_config, ob_config, fvg_config, liquidity_config, session_config, engulfing_config, event_bus),
        }
        self._resamplers = {"M5": IncrementalResampler("5min"), "M15": IncrementalResampler("15min")}
        self.event_bus = event_bus
        self.latest_snapshot = None

    # ------------------------------------------------------------------
    def ingest_m1_candle(self, timestamp, open_, high, low, close) -> dict:
        """The ONLY entry point a live loop needs to call per completed
        M1 candle. Updates all three timeframe streams and returns the
        latest M1 confluence snapshot. No history is rebuilt."""
        idx = len(self._m1_rows)
        candle = Candle(index=idx, timestamp=timestamp, open=open_, high=high, low=low, close=close)
        self._m1_rows.append({"timestamp": timestamp, "open": open_, "high": high, "low": low, "close": close})
        if len(self._m1_rows) > self.m1_history_len:
            self._m1_rows = self._m1_rows[-self.m1_history_len:]

        self.latest_snapshot = self.engines["M1"].process_candle(candle)

        for tf in ("M5", "M15"):
            completed = self._resamplers[tf].ingest(candle)
            if completed is not None:
                self.engines[tf].process_candle(completed)

        return self.latest_snapshot.to_dict() if self.latest_snapshot else {}

    @property
    def m1(self) -> pd.DataFrame:
        return pd.DataFrame(self._m1_rows)

    # ------------------------------------------------------------------
    # MarketContext-compatible accessors (current/live state only)
    # ------------------------------------------------------------------
    # Fallback column sets so an empty DataFrame (no objects created yet,
    # e.g. the first few candles of a fresh live session) still exposes
    # the columns strategies unconditionally reference (`.swept_timestamp`,
    # `.direction`, etc.) instead of raising AttributeError -- pandas gives
    # an empty `pd.DataFrame([])` NO columns at all, unlike the batch
    # feature-detection functions which always return the full fixed schema.
    _OB_COLUMNS = ["ob_id", "symbol", "timeframe", "direction", "low", "high", "creation_timestamp",
                   "creation_index", "current_state", "freshness_status", "first_touch_timestamp",
                   "wick_ratio", "quality_score", "displacement_reference"]
    _LIQUIDITY_COLUMNS = ["liquidity_id", "symbol", "timeframe", "type", "side", "price", "state",
                           "creation_timestamp", "creation_candle_index", "first_touch_timestamp",
                           "swept_timestamp", "strength", "number_of_touches"]
    _FVG_COLUMNS = ["fvg_id", "symbol", "timeframe", "direction", "high", "low", "creation_timestamp",
                    "active_status", "fill_pct"]
    _DISPLACEMENT_COLUMNS = ["displacement_id", "direction", "start_timestamp", "end_timestamp",
                              "start_index", "end_index", "total_range", "num_candles"]

    def candles(self, timeframe: str) -> pd.DataFrame:
        if timeframe == "M1":
            return self.m1
        raise NotImplementedError("LiveMarketContext exposes M5/M15 via their trackers, not a raw candle frame (no strategy currently needs it).")

    def order_blocks(self, timeframe: str) -> pd.DataFrame:
        # Full history (ACTIVE + MITIGATED + archived), matching what the
        # batch MarketContext.order_blocks() returns via detect_order_blocks
        # -- `_objects` is never pruned (see IncrementalOrderBlockTracker's
        # class docstring), so this is the live equivalent of that full
        # dataset, not just the currently-tradeable subset (that's what
        # fresh_order_block_asof is for).
        records = list(self.engines[timeframe].order_blocks._objects.values())
        return pd.DataFrame(records) if records else pd.DataFrame(columns=self._OB_COLUMNS)

    def liquidity(self, timeframe: str) -> pd.DataFrame:
        # Full history including SWEPT/ARCHIVED levels -- S3's swept-level
        # scan (`liquidity[liquidity.swept_timestamp.notna()]`) needs a
        # level to still be visible AFTER it's swept, same reason as
        # order_blocks() above. active_levels() (ACTIVE-only) would make
        # every swept level vanish the instant it's swept, breaking S3
        # entirely on live data.
        records = list(self.engines[timeframe].liquidity._objects.values())
        return pd.DataFrame(records) if records else pd.DataFrame(columns=self._LIQUIDITY_COLUMNS)

    def displacement(self, timeframe: str) -> pd.DataFrame:
        records = self.engines[timeframe].displacement.completed_runs
        return pd.DataFrame(records) if records else pd.DataFrame(columns=self._DISPLACEMENT_COLUMNS)

    def structure_state_asof(self, timeframe: str, timestamp=None) -> str:
        return self.engines[timeframe].structure.state

    def latest_choch_asof(self, timeframe: str, timestamp=None, direction: Optional[str] = None):
        """Current state only (no historical timestamp param needed --
        see class docstring): the most recent CHoCH event on this
        timeframe, matching `direction` if given. Same "scan backward,
        return first match" semantics as the batch MarketContext, just
        over a list that's already bounded to what's happened so far."""
        events = self.engines[timeframe].structure.events
        for ev in reversed(events):
            if ev["event_type"] != "CHoCH":
                continue
            if direction and ev["direction"] != direction:
                continue
            return ev
        return None

    def fresh_order_block_asof(self, timeframe: str, direction: str, timestamp=None, price_near: Optional[float] = None, tolerance: float = 0.01):
        candidates = [
            ob for ob in self.engines[timeframe].order_blocks.active_order_blocks()
            if ob["direction"] == direction and ob["freshness_status"] == "FRESH"
        ]
        if price_near is not None:
            candidates = [
                ob for ob in candidates
                if ob["low"] <= price_near * (1 + tolerance) and ob["high"] >= price_near * (1 - tolerance)
            ]
        if not candidates:
            return None
        return max(candidates, key=lambda ob: ob["creation_timestamp"])

    def fvgs(self, timeframe: str) -> pd.DataFrame:
        return pd.DataFrame(list(self.engines[timeframe].fvgs._objects.values()))

    def active_fvg_asof(self, timeframe: str, direction: str, timestamp=None):
        candidates = [fvg for fvg in self.engines[timeframe].fvgs.active_fvgs() if fvg["direction"] == direction]
        if not candidates:
            return None
        return max(candidates, key=lambda f: f["creation_timestamp"])

    def session_active_asof(self, session_name: str, timestamp=None) -> bool:
        return self.engines["M1"].sessions._current.get(session_name) is not None

    _SESSION_COLUMNS = ["session_name", "start_utc", "end_utc", "high", "low", "session_date"]
    _REFERENCE_LEVEL_COLUMNS = ["level_type", "value", "reference_period_start", "available_from"]
    _WEEKEND_GAP_COLUMNS = ["gap_id", "state", "gap_open", "prior_close", "gap_filled_pct"]

    @property
    def sessions(self) -> pd.DataFrame:
        rows = self.engines["M1"].sessions.all_sessions
        return pd.DataFrame(rows) if rows else pd.DataFrame(columns=self._SESSION_COLUMNS)

    @property
    def reference_levels(self) -> pd.DataFrame:
        rows = self.engines["M1"].reference_levels.history
        return pd.DataFrame(rows) if rows else pd.DataFrame(columns=self._REFERENCE_LEVEL_COLUMNS)

    @property
    def weekend_gaps(self) -> pd.DataFrame:
        rows = self.engines["M1"].reference_levels.weekend_gaps
        return pd.DataFrame(rows) if rows else pd.DataFrame(columns=self._WEEKEND_GAP_COLUMNS)
