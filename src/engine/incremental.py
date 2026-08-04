"""
Incremental (streaming) trackers for every SMC feature, mirroring the
semantics already specified/tested in src/structure/ and src/features/ but
processing ONE NEW CANDLE AT A TIME instead of recomputing over the full
history.

Design principle: each tracker only does work proportional to the number
of currently ACTIVE objects it holds (or O(1)/O(small-constant) for the
detection step itself), never proportional to the full length of history
processed so far. This is what makes the engine suitable for millions of
M1 candles across multiple symbols -- see docs/CONFLUENCE_ENGINE.md for the
full complexity discussion and docs/MARKET_STRUCTURE_SPEC.md /
docs/SMC_FEATURE_ENGINE.md for the underlying definitions this must stay
faithful to.

Nothing here modifies src/structure/ or src/features/ -- those remain the
batch/research reference implementation. This module is a parallel,
streaming-oriented implementation of the same rules, cross-checked for
equivalence in tests/test_incremental_engine.py.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

import pandas as pd

from config.settings import (
    SwingConfig, StructureConfig, DisplacementConfig, OrderBlockConfig,
    FVGConfig, LiquidityConfig, SessionConfig, EngulfingConfig,
)
from src.engine.event_bus import EventBus, EventType


@dataclass
class Candle:
    index: int
    timestamp: pd.Timestamp
    open: float
    high: float
    low: float
    close: float

    @property
    def direction(self) -> str:
        if self.close > self.open:
            return "bullish"
        if self.close < self.open:
            return "bearish"
        return "flat"

    @property
    def body_size(self) -> float:
        return abs(self.close - self.open)

    @property
    def range(self) -> float:
        return self.high - self.low


# ---------------------------------------------------------------------------
# 1. Swing tracker
# ---------------------------------------------------------------------------


class IncrementalSwingTracker:
    """O(1) amortized per candle: a fixed-size rolling window of
    left+right+1 candles is all that is needed to confirm a swing, exactly
    as in src.structure.swings.detect_swings but without ever looking at
    candles older than the window."""

    def __init__(self, config: SwingConfig, interval: pd.Timedelta, timeframe: str, event_bus: Optional[EventBus] = None):
        self.config = config
        self.interval = interval
        self.timeframe = timeframe
        self.event_bus = event_bus
        self._window: deque[Candle] = deque(maxlen=config.left + config.right + 1)
        self.confirmed_swings: list[dict] = []  # full history retained, never deleted
        self._seq = 0

    def update(self, candle: Candle) -> list[dict]:
        self._window.append(candle)
        new_swings = []
        if len(self._window) < self._window.maxlen:
            return new_swings

        win = list(self._window)
        left = self.config.left
        center = win[left]
        left_highs = [c.high for c in win[:left]]
        right_highs = [c.high for c in win[left + 1:]]
        left_lows = [c.low for c in win[:left]]
        right_lows = [c.low for c in win[left + 1:]]

        confirmed_timestamp = candle.timestamp + self.interval

        if center.high > max(left_highs) and center.high > max(right_highs):
            self._seq += 1
            swing = {
                "swing_id": f"SWING_{self.timeframe}_{self._seq}",
                "swing_timestamp": center.timestamp,
                "confirmed_timestamp": confirmed_timestamp,
                "price": center.high,
                "swing_type": "high",
                "timeframe": self.timeframe,
                "candle_index": center.index,
            }
            self.confirmed_swings.append(swing)
            new_swings.append(swing)
            if self.event_bus:
                self.event_bus.publish(EventType.SWING_CONFIRMED, candle.timestamp, swing)

        if center.low < min(left_lows) and center.low < min(right_lows):
            self._seq += 1
            swing = {
                "swing_id": f"SWING_{self.timeframe}_{self._seq}",
                "swing_timestamp": center.timestamp,
                "confirmed_timestamp": confirmed_timestamp,
                "price": center.low,
                "swing_type": "low",
                "timeframe": self.timeframe,
                "candle_index": center.index,
            }
            self.confirmed_swings.append(swing)
            new_swings.append(swing)
            if self.event_bus:
                self.event_bus.publish(EventType.SWING_CONFIRMED, candle.timestamp, swing)

        return new_swings

    def state_dict(self) -> dict:
        return {"window": [asdict(c) for c in self._window], "confirmed_swings": self.confirmed_swings, "seq": self._seq}

    def restore(self, state: dict) -> None:
        self._window = deque((Candle(**c) for c in state["window"]), maxlen=self.config.left + self.config.right + 1)
        self.confirmed_swings = state["confirmed_swings"]
        self._seq = state["seq"]


# ---------------------------------------------------------------------------
# 2. Market structure (BOS/CHoCH) tracker
# ---------------------------------------------------------------------------


class IncrementalStructureTracker:
    """Same rules as src.structure.market_structure.detect_structure_events,
    fed one confirmed swing / one candle close at a time. O(1) per candle."""

    def __init__(self, config: StructureConfig, symbol: str, timeframe: str, event_bus: Optional[EventBus] = None):
        self.config = config
        self.symbol = symbol
        self.timeframe = timeframe
        self.event_bus = event_bus
        self.active_high: Optional[dict] = None
        self.active_low: Optional[dict] = None
        self.state = "UNKNOWN"
        self._broken_high_keys: set = set()
        self._broken_low_keys: set = set()
        self.events: list[dict] = []
        self._seq = 0

    def ingest_swing(self, swing: dict) -> None:
        key = (swing["swing_type"], round(float(swing["price"]), 10), swing["swing_timestamp"])
        if swing["swing_type"] == "high":
            if key not in self._broken_high_keys and (
                self.active_high is None or swing["candle_index"] > self.active_high["candle_index"]
            ):
                self.active_high = dict(swing)
        else:
            if key not in self._broken_low_keys and (
                self.active_low is None or swing["candle_index"] > self.active_low["candle_index"]
            ):
                self.active_low = dict(swing)

    def update(self, candle: Candle) -> list[dict]:
        new_events = []
        require_close = self.config.require_close_beyond_level

        if self.active_high is not None and (
            (require_close and candle.close > self.active_high["price"])
            or (not require_close and candle.high > self.active_high["price"])
        ):
            previous_state = self.state
            event_type = "CHoCH" if previous_state == "BEARISH" else "BOS"
            self.state = "BULLISH"
            self._seq += 1
            ev = {
                "event_id": f"{self.symbol}_{self.timeframe}_{self._seq}",
                "symbol": self.symbol, "timeframe": self.timeframe,
                "event_type": event_type, "direction": "bullish",
                "broken_level": self.active_high["price"],
                "broken_swing_timestamp": self.active_high["swing_timestamp"],
                "confirmation_timestamp": self.active_high["confirmed_timestamp"],
                "break_candle_timestamp": candle.timestamp, "break_candle_close": candle.close,
                "previous_structure_state": previous_state, "new_structure_state": self.state,
            }
            self.events.append(ev)
            new_events.append(ev)
            self._broken_high_keys.add((
                "high", round(self.active_high["price"], 10), self.active_high["swing_timestamp"]
            ))
            self.active_high = None
            if self.event_bus:
                etype = EventType.BULLISH_CHOCH if event_type == "CHoCH" else EventType.BULLISH_BOS
                self.event_bus.publish(etype, candle.timestamp, ev)

        if self.active_low is not None and (
            (require_close and candle.close < self.active_low["price"])
            or (not require_close and candle.low < self.active_low["price"])
        ):
            previous_state = self.state
            event_type = "CHoCH" if previous_state == "BULLISH" else "BOS"
            self.state = "BEARISH"
            self._seq += 1
            ev = {
                "event_id": f"{self.symbol}_{self.timeframe}_{self._seq}",
                "symbol": self.symbol, "timeframe": self.timeframe,
                "event_type": event_type, "direction": "bearish",
                "broken_level": self.active_low["price"],
                "broken_swing_timestamp": self.active_low["swing_timestamp"],
                "confirmation_timestamp": self.active_low["confirmed_timestamp"],
                "break_candle_timestamp": candle.timestamp, "break_candle_close": candle.close,
                "previous_structure_state": previous_state, "new_structure_state": self.state,
            }
            self.events.append(ev)
            new_events.append(ev)
            self._broken_low_keys.add((
                "low", round(self.active_low["price"], 10), self.active_low["swing_timestamp"]
            ))
            self.active_low = None
            if self.event_bus:
                etype = EventType.BEARISH_CHOCH if event_type == "CHoCH" else EventType.BEARISH_BOS
                self.event_bus.publish(etype, candle.timestamp, ev)

        return new_events

    def state_dict(self) -> dict:
        return {
            "active_high": self.active_high, "active_low": self.active_low, "state": self.state,
            "broken_high_keys": list(self._broken_high_keys), "broken_low_keys": list(self._broken_low_keys),
            "events": self.events, "seq": self._seq,
        }

    def restore(self, state: dict) -> None:
        self.active_high = state["active_high"]
        self.active_low = state["active_low"]
        self.state = state["state"]
        self._broken_high_keys = set(tuple(k) for k in state["broken_high_keys"])
        self._broken_low_keys = set(tuple(k) for k in state["broken_low_keys"])
        self.events = state["events"]
        self._seq = state["seq"]


# ---------------------------------------------------------------------------
# 3. Displacement tracker (rolling ATR / avg body, O(1) per candle)
# ---------------------------------------------------------------------------


class IncrementalDisplacementTracker:
    def __init__(self, config: DisplacementConfig):
        self.config = config
        self._tr_window: deque[float] = deque(maxlen=config.atr_period)
        self._body_window: deque[float] = deque(maxlen=config.recent_body_lookback)
        self._prev_close: Optional[float] = None
        self._run_direction: Optional[str] = None
        self._run_start: Optional[Candle] = None
        self._run_last: Optional[Candle] = None
        self._run_total_range: float = 0.0
        self.completed_runs: list[dict] = []
        self._seq = 0

    def _atr(self) -> float:
        return sum(self._tr_window) / len(self._tr_window) if self._tr_window else 0.0

    def _avg_body(self) -> Optional[float]:
        return sum(self._body_window) / len(self._body_window) if self._body_window else None

    def update(self, candle: Candle) -> tuple[Optional[dict], Optional[dict]]:
        """Returns `(active_run, completed_run)`.

        `active_run` is a dict describing the in-progress run as of THIS
        candle (start_index/end_index=candle.index/direction) whenever
        this candle continues or starts a qualifying run -- consumers
        (the Order Block tracker) use this to create/update an Order Block
        as early as the information allows, rather than waiting for a
        "closing" candle that, in a live stream, may never arrive (the
        batch reference implementation can afford to wait because it has
        the whole history already; a streaming engine cannot).

        `completed_run` is populated only at the moment a run is
        determined to have ended (a non-qualifying or opposite-direction
        candle arrives), and is kept purely for audit/history purposes."""
        true_range = candle.range if self._prev_close is None else max(
            candle.high - candle.low, abs(candle.high - self._prev_close), abs(candle.low - self._prev_close),
        )
        atr = self._atr()
        avg_body = self._avg_body()

        cond1 = bool(atr > 0 and candle.body_size >= self.config.atr_multiplier * atr)
        cond2 = bool(avg_body and avg_body > 0 and candle.body_size >= self.config.body_multiple * avg_body)
        body_ratio = (candle.body_size / candle.range) if candle.range > 0 else 0.0
        cond3 = bool(body_ratio >= self.config.min_body_ratio)
        qualifies = candle.direction != "flat" and (sum([cond1, cond2, cond3]) >= self.config.min_conditions_met)

        completed = None
        active_run = None
        if qualifies and self._run_direction == candle.direction:
            self._run_last = candle
            self._run_total_range += true_range
        else:
            if self._run_direction is not None:
                completed = self._finalize_run()
            if qualifies:
                self._run_direction = candle.direction
                self._run_start = candle
                self._run_last = candle
                self._run_total_range = true_range
            else:
                self._run_direction = None
                self._run_start = None
                self._run_last = None
                self._run_total_range = 0.0

        if self._run_direction is not None:
            active_run = {
                "displacement_id": f"disp_active_{self._run_start.index}",
                "direction": self._run_direction,
                "start_index": self._run_start.index, "end_index": self._run_last.index,
                "start_timestamp": self._run_start.timestamp, "end_timestamp": self._run_last.timestamp,
                "total_range": self._run_total_range,
                "num_candles": self._run_last.index - self._run_start.index + 1,
            }

        self._tr_window.append(true_range)
        self._body_window.append(candle.body_size)
        self._prev_close = candle.close
        return active_run, completed

    def flush(self) -> Optional[dict]:
        """Force-finalize any in-progress run (e.g. at end of stream)."""
        if self._run_direction is not None:
            return self._finalize_run()
        return None

    def _finalize_run(self) -> dict:
        self._seq += 1
        run = {
            "displacement_id": f"disp_{self._seq}",
            "direction": self._run_direction,
            "start_index": self._run_start.index,
            "end_index": self._run_last.index,
            "start_timestamp": self._run_start.timestamp,
            "end_timestamp": self._run_last.timestamp,
            "total_range": self._run_total_range,
            "num_candles": self._run_last.index - self._run_start.index + 1,
        }
        self.completed_runs.append(run)
        self._run_direction = None
        self._run_start = None
        self._run_last = None
        self._run_total_range = 0.0
        return run

    def state_dict(self) -> dict:
        return {
            "tr_window": list(self._tr_window), "body_window": list(self._body_window),
            "prev_close": self._prev_close,
            "run_direction": self._run_direction,
            "run_start": asdict(self._run_start) if self._run_start else None,
            "run_last": asdict(self._run_last) if self._run_last else None,
            "run_total_range": self._run_total_range,
            "completed_runs": self.completed_runs, "seq": self._seq,
        }

    def restore(self, state: dict) -> None:
        self._tr_window = deque(state["tr_window"], maxlen=self.config.atr_period)
        self._body_window = deque(state["body_window"], maxlen=self.config.recent_body_lookback)
        self._prev_close = state["prev_close"]
        self._run_direction = state["run_direction"]
        self._run_start = Candle(**state["run_start"]) if state["run_start"] else None
        self._run_last = Candle(**state["run_last"]) if state["run_last"] else None
        self._run_total_range = state.get("run_total_range", 0.0)
        self.completed_runs = state["completed_runs"]
        self._seq = state["seq"]


# ---------------------------------------------------------------------------
# 4. Order Block tracker
# ---------------------------------------------------------------------------


class IncrementalOrderBlockTracker:
    """
    Order Blocks are created as soon as a displacement RUN STARTS (using
    `active_run` from IncrementalDisplacementTracker), not once it
    "completes" -- a streaming engine cannot wait for a closing candle
    that, live, may never come within the current session. Each run is
    keyed by its `start_index`, so if the run extends over several more
    candles, the SAME Order Block's `displacement_reference` is simply
    updated in place (no duplicate object, exactly one OrderBlockCreated
    event per run).

    All objects are stored in a dict keyed by `ob_id` (`self._objects`)
    for O(1) lookup -- looking an object up by scanning a list on every
    active-object update, every candle, is exactly the kind of hidden
    O(history) cost this task exists to eliminate.
    """

    def __init__(self, config: OrderBlockConfig, symbol: str, timeframe: str, event_bus: Optional[EventBus] = None):
        self.config = config
        self.symbol = symbol
        self.timeframe = timeframe
        self.event_bus = event_bus
        self._recent_candles: deque[Candle] = deque(maxlen=max(config.lookback_candles + 5, 10))
        self._objects: dict[str, dict] = {}       # ob_id -> OB dict, never deleted
        self._run_start_to_ob_id: dict[int, str] = {}
        self._active_ids: set[str] = set()
        self._pending_invalidation: dict[str, int] = {}  # ob_id -> index deadline
        self._pending_archive: dict[str, int] = {}       # ob_id -> terminal index
        self.skipped: list[dict] = []
        self._seq = 0

    @property
    def all_order_blocks(self) -> list[dict]:
        return list(self._objects.values())

    def on_displacement_active(self, run: dict, atr: float = 0.0) -> Optional[dict]:
        """Called every candle a displacement run is in progress (including
        its very first candle). Creates the OB on first sight of the run;
        on subsequent candles of the same run, just refreshes the
        displacement_reference end/timestamp in place.

        `atr`: the rolling ATR at the moment of creation (from
        IncrementalDisplacementTracker._atr()), stored once and reused by
        `update()`'s quality_score calculation -- see the module-level
        note in Task 11 Phase 3 docs about why this parameter exists:
        the ORIGINAL incremental tracker left `quality_score` permanently
        `None` (a gap vs. the batch `src.features.order_blocks` reference
        implementation, which DOES compute it), discovered while wiring
        Task 9/10's ITQS/IOS (both depend on OB quality_score) to live
        data. Fixed properly here rather than worked around downstream."""
        existing_id = self._run_start_to_ob_id.get(run["start_index"])
        if existing_id is not None:
            ob = self._objects[existing_id]
            ob["displacement_reference"] = run
            return ob

        opposite = "bearish" if run["direction"] == "bullish" else "bullish"
        ob_candle = None
        for c in reversed(self._recent_candles):
            if c.index >= run["start_index"]:
                continue
            if c.index < run["start_index"] - self.config.lookback_candles:
                break
            if c.direction == opposite:
                ob_candle = c
                break

        if ob_candle is None:
            self.skipped.append({"displacement_id": run["displacement_id"], "reason": "no opposite-direction candle in lookback"})
            self._run_start_to_ob_id[run["start_index"]] = None  # remember we already tried, don't retry every candle
            return None

        self._seq += 1
        wick_ratio = ((ob_candle.range - ob_candle.body_size) / ob_candle.range) if ob_candle.range > 0 else 0.0
        ob_id = f"{self.symbol}_{self.timeframe}_OB_{self._seq}"
        ob = {
            "ob_id": ob_id,
            "direction": run["direction"], "timeframe": self.timeframe,
            "creation_timestamp": ob_candle.timestamp, "creation_index": ob_candle.index,
            "high": ob_candle.high, "low": ob_candle.low, "open": ob_candle.open, "close": ob_candle.close,
            "body_size": ob_candle.body_size, "wick_ratio": wick_ratio,
            "displacement_reference": run,
            "freshness_status": "FRESH", "mitigation_status": "UNMITIGATED",
            "first_touch_timestamp": None, "full_mitigation_timestamp": None,
            "current_state": "ACTIVE", "quality_score": None,
            "_full_mitigation_index": None, "_atr_at_creation": atr,
        }
        self._objects[ob_id] = ob
        self._run_start_to_ob_id[run["start_index"]] = ob_id
        self._active_ids.add(ob_id)
        if self.event_bus:
            self.event_bus.publish(EventType.ORDER_BLOCK_CREATED, ob_candle.timestamp, ob)
        return ob

    def update(self, candle: Candle, structure_events_this_candle: list[dict]) -> None:
        self._recent_candles.append(candle)

        for ob_id in list(self._active_ids):
            ob = self._objects[ob_id]
            # Skip mitigation checks for every candle still inside the SAME
            # displacement run that created this OB -- not just the OB's own
            # creation candle. Since the OB is now created as soon as the run
            # STARTS (on_displacement_active), and `displacement_reference`
            # is refreshed to the current candle every candle the run keeps
            # extending, `end_index` tracks the run's last-known extent. Only
            # once a candle arrives that is NOT part of that run does it
            # represent the "return move" mitigation is actually about --
            # exactly the same reasoning as the batch engine's fix (see
            # src/features/order_blocks.py) for why the impulsive leg itself
            # must never count as a "touch".
            if candle.index <= ob["displacement_reference"]["end_index"]:
                continue
            direction = ob["direction"]
            changed_terminal = False

            if direction == "bullish":
                touched = candle.low <= ob["high"]
                closed_through = candle.close < ob["low"]
            else:
                touched = candle.high >= ob["low"]
                closed_through = candle.close > ob["high"]

            if touched and ob["current_state"] == "ACTIVE":
                ob["current_state"] = "PARTIALLY_MITIGATED"
                ob["mitigation_status"] = "PARTIAL"
                ob["freshness_status"] = "MITIGATED"
                ob["first_touch_timestamp"] = candle.timestamp

            if closed_through and ob["current_state"] in ("ACTIVE", "PARTIALLY_MITIGATED"):
                ob["current_state"] = "FULLY_MITIGATED"
                ob["mitigation_status"] = "FULL"
                ob["freshness_status"] = "MITIGATED"
                ob["full_mitigation_timestamp"] = candle.timestamp
                ob["_full_mitigation_index"] = candle.index
                self._pending_invalidation[ob_id] = candle.index + self.config.invalidation_lookahead
                if self.event_bus:
                    self.event_bus.publish(EventType.ORDER_BLOCK_MITIGATED, candle.timestamp, ob)
                changed_terminal = True

            if ob_id in self._pending_invalidation:
                deadline = self._pending_invalidation[ob_id]
                opposing_direction = "bearish" if direction == "bullish" else "bullish"
                for ev in structure_events_this_candle:
                    if ev["direction"] == opposing_direction and ev["break_candle_timestamp"] > ob["full_mitigation_timestamp"]:
                        ob["current_state"] = "INVALIDATED"
                        if self.event_bus:
                            self.event_bus.publish(EventType.ORDER_BLOCK_INVALIDATED, candle.timestamp, ob)
                        del self._pending_invalidation[ob_id]
                        changed_terminal = True
                        break
                if candle.index >= deadline and ob_id in self._pending_invalidation:
                    del self._pending_invalidation[ob_id]

            if changed_terminal and ob["current_state"] in ("FULLY_MITIGATED", "INVALIDATED"):
                self._pending_archive[ob_id] = candle.index

            if ob_id in self._pending_archive:
                if candle.index - self._pending_archive[ob_id] >= self.config.archive_after_candles:
                    ob["current_state"] = "ARCHIVED"
                    self._active_ids.discard(ob_id)
                    del self._pending_archive[ob_id]

            # Same formula as src.features.order_blocks' batch reference
            # implementation, recomputed every candle an OB stays active
            # (age_score and freshness_score are both functions of "now"),
            # not just at creation -- see on_displacement_active's
            # docstring for why this was missing here originally.
            freshness_score = 1.0 if ob["current_state"] == "ACTIVE" else (0.5 if ob["current_state"] == "PARTIALLY_MITIGATED" else 0.0)
            run = ob["displacement_reference"]
            atr_at_creation = ob.get("_atr_at_creation") or 0.0
            displacement_score = min(run["total_range"] / (atr_at_creation * 3), 1.0) if atr_at_creation > 0 else 0.0
            body_score = 1.0 - ob["wick_ratio"]
            age_candles = candle.index - ob["creation_index"]
            age_score = max(0.0, 1.0 - age_candles / max(self.config.archive_after_candles, 1))
            ob["quality_score"] = round(0.30 * freshness_score + 0.30 * displacement_score + 0.20 * body_score + 0.20 * age_score, 4)

    def active_order_blocks(self) -> list[dict]:
        """Returns SHALLOW COPIES of currently active OBs. Copying is
        essential here: these dicts are mutated in place every candle by
        `update()` above, so handing out live references would let a
        confluence snapshot silently change after the fact once more
        candles are processed -- exactly the immutability bug this task's
        tests guard against."""
        return [dict(ob) for ob in self._objects.values() if ob["current_state"] in ("ACTIVE", "PARTIALLY_MITIGATED")]

    def state_dict(self) -> dict:
        return {
            "recent_candles": [asdict(c) for c in self._recent_candles],
            "all_order_blocks": list(self._objects.values()),
            "run_start_to_ob_id": {str(k): v for k, v in self._run_start_to_ob_id.items()},
            "active_ids": list(self._active_ids),
            "pending_invalidation": self._pending_invalidation,
            "pending_archive": self._pending_archive,
            "skipped": self.skipped, "seq": self._seq,
        }

    def restore(self, state: dict) -> None:
        maxlen = max(self.config.lookback_candles + 5, 10)
        self._recent_candles = deque((Candle(**c) for c in state["recent_candles"]), maxlen=maxlen)
        self._objects = {ob["ob_id"]: ob for ob in state["all_order_blocks"]}
        self._run_start_to_ob_id = {int(k): v for k, v in state["run_start_to_ob_id"].items()}
        self._active_ids = set(state["active_ids"])
        self._pending_invalidation = {k: v for k, v in state["pending_invalidation"].items()}
        self._pending_archive = {k: v for k, v in state["pending_archive"].items()}
        self.skipped = state["skipped"]
        self._seq = state["seq"]


# ---------------------------------------------------------------------------
# 5. FVG tracker
# ---------------------------------------------------------------------------


class IncrementalFVGTracker:
    """Stores objects in a dict keyed by `fvg_id` for O(1) lookup -- see
    the equivalent note on IncrementalOrderBlockTracker for why this
    matters (a per-candle O(active) loop that does an O(all-time-created)
    linear search inside it is secretly O(n^2), not O(n))."""

    def __init__(self, config: FVGConfig, symbol: str, timeframe: str, interval: pd.Timedelta, event_bus: Optional[EventBus] = None):
        self.config = config
        self.symbol = symbol
        self.timeframe = timeframe
        self.interval = interval
        self.event_bus = event_bus
        self._window: deque[Candle] = deque(maxlen=3)
        self._objects: dict[str, dict] = {}
        self._active_ids: set[str] = set()
        self._deepest: dict[str, float] = {}
        self._seq = 0

    @property
    def all_fvgs(self) -> list[dict]:
        return list(self._objects.values())

    def update(self, candle: Candle) -> None:
        self._window.append(candle)

        if len(self._window) == 3:
            c0, c1, c2 = self._window
            bullish_gap = c0.high < c2.low
            bearish_gap = c0.low > c2.high
            if bullish_gap or bearish_gap:
                direction = "bullish" if bullish_gap else "bearish"
                bottom = c0.high if bullish_gap else c2.high
                top = c2.low if bullish_gap else c0.low
                size = top - bottom
                if size > self.config.min_gap_size:
                    self._seq += 1
                    creation_timestamp = c2.timestamp + self.interval
                    fvg = {
                        "fvg_id": f"{self.symbol}_{self.timeframe}_FVG_{self._seq}",
                        "direction": direction, "timeframe": self.timeframe,
                        "top": top, "bottom": bottom, "size": size,
                        "consequent_encroachment": bottom + size / 2, "ce_reached": False,
                        "creation_timestamp": creation_timestamp, "creation_index": c2.index,
                        "impulse_candle_index": c1.index,
                        "filled_percentage": 0.0, "mitigation_state": "ACTIVE",
                        "age": 0, "active_status": "ACTIVE",
                    }
                    self._objects[fvg["fvg_id"]] = fvg
                    self._active_ids.add(fvg["fvg_id"])
                    self._deepest[fvg["fvg_id"]] = 0.0
                    if self.event_bus:
                        self.event_bus.publish(EventType.FVG_CREATED, creation_timestamp, fvg)

        for fvg_id in list(self._active_ids):
            fvg = self._objects[fvg_id]
            if candle.index <= fvg["creation_index"]:
                continue
            size = fvg["size"]
            if fvg["direction"] == "bullish":
                penetration = max(0.0, min(fvg["top"] - candle.low, size))
                if candle.low <= fvg["consequent_encroachment"]:
                    fvg["ce_reached"] = True
            else:
                penetration = max(0.0, min(candle.high - fvg["bottom"], size))
                if candle.high >= fvg["consequent_encroachment"]:
                    fvg["ce_reached"] = True

            self._deepest[fvg_id] = max(self._deepest[fvg_id], penetration)
            filled_pct = round(100.0 * self._deepest[fvg_id] / size, 2) if size > 0 else 0.0
            fvg["filled_percentage"] = filled_pct
            fvg["age"] = candle.index - fvg["creation_index"]

            was_active = fvg["active_status"]
            if filled_pct >= 100.0:
                fvg["active_status"] = "FULLY_MITIGATED"
                fvg["mitigation_state"] = "FULLY_MITIGATED"
                if was_active != "FULLY_MITIGATED" and self.event_bus:
                    self.event_bus.publish(EventType.FVG_MITIGATED, candle.timestamp, fvg)
                self._active_ids.discard(fvg_id)
            elif filled_pct > 0.0:
                fvg["active_status"] = "PARTIALLY_FILLED"
                fvg["mitigation_state"] = "PARTIALLY_FILLED"
            elif fvg["age"] >= self.config.expire_after_candles:
                fvg["active_status"] = "EXPIRED"
                fvg["mitigation_state"] = "EXPIRED"
                self._active_ids.discard(fvg_id)

    def active_fvgs(self) -> list[dict]:
        """Returns SHALLOW COPIES -- see IncrementalOrderBlockTracker.active_order_blocks
        for why copying (not returning live references) is required here."""
        return [dict(f) for f in self._objects.values() if f["active_status"] in ("ACTIVE", "PARTIALLY_FILLED")]

    def state_dict(self) -> dict:
        return {
            "window": [asdict(c) for c in self._window], "all_fvgs": list(self._objects.values()),
            "active_ids": list(self._active_ids), "deepest": self._deepest, "seq": self._seq,
        }

    def restore(self, state: dict) -> None:
        self._window = deque((Candle(**c) for c in state["window"]), maxlen=3)
        self._objects = {f["fvg_id"]: f for f in state["all_fvgs"]}
        self._active_ids = set(state["active_ids"])
        self._deepest = state["deepest"]
        self._seq = state["seq"]


# ---------------------------------------------------------------------------
# 6. Liquidity tracker
# ---------------------------------------------------------------------------


class IncrementalLiquidityTracker:
    """Stores objects in a dict keyed by `liquidity_id` for O(1) lookup --
    see the equivalent note on IncrementalOrderBlockTracker. The equal-
    level clustering search in `ingest_swing` is only O(active levels of
    the same side), since only swings need to check for a nearby existing
    level, and only ever once per confirmed swing (swings arrive one at a
    time, not once per candle)."""

    def __init__(self, config: LiquidityConfig, symbol: str, timeframe: str, event_bus: Optional[EventBus] = None):
        self.config = config
        self.symbol = symbol
        self.timeframe = timeframe
        self.event_bus = event_bus
        self._objects: dict[str, dict] = {}
        self._active_ids: set[str] = set()
        self._running_high_extreme: Optional[float] = None
        self._running_low_extreme: Optional[float] = None
        self._seq = 0

    @property
    def all_levels(self) -> list[dict]:
        return list(self._objects.values())

    def ingest_swing(self, swing: dict) -> None:
        swing_type = swing["swing_type"]
        side = "buy_side" if swing_type == "high" else "sell_side"
        price = swing["price"]
        same_side_types = ("swing_high", "equal_high") if swing_type == "high" else ("swing_low", "equal_low")

        merged_lvl = None
        for liq_id in self._active_ids:
            lvl = self._objects[liq_id]
            if lvl["type"] not in same_side_types or lvl["state"] != "ACTIVE":
                continue
            tolerance = self.config.equal_level_tolerance * lvl["price"]
            if abs(price - lvl["price"]) <= tolerance:
                n = lvl["number_of_touches"]
                lvl["price"] = (lvl["price"] * n + price) / (n + 1)
                lvl["number_of_touches"] = n + 1
                lvl["type"] = "equal_high" if swing_type == "high" else "equal_low"
                lvl["strength"] = "strong" if lvl["number_of_touches"] >= self.config.min_touches_for_strength else "weak"
                merged_lvl = lvl
                break

        if swing_type == "high":
            self._running_high_extreme = price if self._running_high_extreme is None else max(self._running_high_extreme, price)
            extreme = self._running_high_extreme
        else:
            self._running_low_extreme = price if self._running_low_extreme is None else min(self._running_low_extreme, price)
            extreme = self._running_low_extreme

        if merged_lvl is None:
            self._seq += 1
            level = {
                "liquidity_id": f"{self.symbol}_{self.timeframe}_LIQ_{self._seq}",
                "type": "swing_high" if swing_type == "high" else "swing_low",
                "side": side, "scope": "external" if price == extreme else "internal",
                "price": price, "number_of_touches": 1,
                "strength": "weak", "creation_timestamp": swing["confirmed_timestamp"],
                "creation_candle_index": swing["candle_index"],
                "first_touch_timestamp": None, "swept_timestamp": None, "state": "ACTIVE",
            }
            self._objects[level["liquidity_id"]] = level
            self._active_ids.add(level["liquidity_id"])
            if self.event_bus:
                self.event_bus.publish(EventType.LIQUIDITY_CREATED, swing["confirmed_timestamp"], level)
        else:
            merged_lvl["scope"] = "external" if merged_lvl["price"] == extreme else merged_lvl["scope"]

    def update(self, candle: Candle) -> None:
        for liq_id in list(self._active_ids):
            lvl = self._objects[liq_id]
            if candle.index <= lvl["creation_candle_index"]:
                continue
            price = lvl["price"]
            if lvl["side"] == "buy_side":
                touched = candle.high >= price
                swept = candle.high > price and candle.close < price
            else:
                touched = candle.low <= price
                swept = candle.low < price and candle.close > price

            if touched and lvl["first_touch_timestamp"] is None:
                lvl["first_touch_timestamp"] = candle.timestamp

            if swept and lvl["state"] == "ACTIVE":
                lvl["state"] = "SWEPT"
                lvl["swept_timestamp"] = candle.timestamp
                lvl["_swept_index"] = candle.index
                if self.event_bus:
                    self.event_bus.publish(EventType.LIQUIDITY_SWEPT, candle.timestamp, lvl)

            if lvl["state"] == "SWEPT" and (candle.index - lvl.get("_swept_index", candle.index)) >= self.config.archive_after_candles:
                lvl["state"] = "ARCHIVED"
                self._active_ids.discard(liq_id)

    def active_levels(self) -> list[dict]:
        """Returns SHALLOW COPIES -- see IncrementalOrderBlockTracker.active_order_blocks
        for why copying (not returning live references) is required here."""
        return [dict(l) for l in self._objects.values() if l["state"] == "ACTIVE"]

    def state_dict(self) -> dict:
        return {
            "all_levels": list(self._objects.values()), "active_ids": list(self._active_ids),
            "running_high_extreme": self._running_high_extreme, "running_low_extreme": self._running_low_extreme,
            "seq": self._seq,
        }

    def restore(self, state: dict) -> None:
        self._objects = {l["liquidity_id"]: l for l in state["all_levels"]}
        self._active_ids = set(state["active_ids"])
        self._running_high_extreme = state["running_high_extreme"]
        self._running_low_extreme = state["running_low_extreme"]
        self._seq = state["seq"]


# ---------------------------------------------------------------------------
# 7. Session tracker
# ---------------------------------------------------------------------------


class IncrementalSessionTracker:
    def __init__(self, config: SessionConfig, event_bus: Optional[EventBus] = None):
        self.config = config
        self.event_bus = event_bus
        self._current: dict[str, Optional[dict]] = {name: None for name in config.windows}
        self.all_sessions: list[dict] = []

    def update(self, candle: Candle) -> dict[str, str]:
        """Returns {session_name: current_status} for this candle."""
        status = {}
        for name, window in self.config.windows.items():
            tz = window["tz"]
            local_ts = candle.timestamp.tz_convert(tz)
            local_time = local_ts.time()
            start_t = pd.Timestamp(f"{local_ts.date()} {window['start']}").time()
            end_t = pd.Timestamp(f"{local_ts.date()} {window['end']}").time()
            in_window = start_t <= local_time < end_t

            cur = self._current[name]
            if in_window:
                if cur is None or cur["local_date"] != local_ts.date():
                    if cur is not None:
                        self._finalize_session(name, cur)
                    cur = {
                        "session_id": f"{name}_{local_ts.date()}", "session_name": name,
                        "local_date": local_ts.date(), "timezone": tz,
                        "start_utc": candle.timestamp, "end_utc": None,
                        "open": candle.open, "high": candle.high, "low": candle.low, "close": candle.close,
                        "num_candles": 1,
                    }
                    self._current[name] = cur
                    if self.event_bus:
                        self.event_bus.publish(EventType.SESSION_STARTED, candle.timestamp, dict(cur))
                    status[name] = "started"
                else:
                    cur["high"] = max(cur["high"], candle.high)
                    cur["low"] = min(cur["low"], candle.low)
                    cur["close"] = candle.close
                    cur["num_candles"] += 1
                    status[name] = "active"
            else:
                if cur is not None:
                    self._finalize_session(name, cur, end_ts=candle.timestamp)
                    self._current[name] = None
                    status[name] = "ended"
                else:
                    status[name] = "closed"
        return status

    def _finalize_session(self, name: str, session: dict, end_ts=None) -> None:
        session = dict(session)
        session["end_utc"] = end_ts if end_ts is not None else session["start_utc"]
        self.all_sessions.append(session)
        if self.event_bus:
            self.event_bus.publish(EventType.SESSION_ENDED, session["end_utc"], session)

    def current_sessions(self) -> dict[str, Optional[dict]]:
        return dict(self._current)

    def state_dict(self) -> dict:
        def _ser(s):
            if s is None:
                return None
            d = dict(s)
            d["local_date"] = str(d["local_date"])
            return d
        return {
            "current": {k: _ser(v) for k, v in self._current.items()},
            "all_sessions": [_ser(s) for s in self.all_sessions],
        }

    def restore(self, state: dict) -> None:
        def _deser(d):
            if d is None:
                return None
            d = dict(d)
            d["local_date"] = pd.Timestamp(d["local_date"]).date()
            return d
        self._current = {k: _deser(v) for k, v in state["current"].items()}
        self.all_sessions = [_deser(s) for s in state["all_sessions"]]


# ---------------------------------------------------------------------------
# 8. Reference level tracker (PDH/PDL/PWH/PWL + weekend gap)
# ---------------------------------------------------------------------------


class IncrementalReferenceLevelTracker:
    def __init__(self, event_bus: Optional[EventBus] = None, min_gap_hours: float = 20.0):
        self.event_bus = event_bus
        self.min_gap_hours = min_gap_hours
        self._current_day = None
        self._day_high = None
        self._day_low = None
        self._current_week = None
        self._week_high = None
        self._week_low = None
        self.pdh: Optional[dict] = None
        self.pdl: Optional[dict] = None
        self.pwh: Optional[dict] = None
        self.pwl: Optional[dict] = None
        self.history: list[dict] = []
        self._prev_candle: Optional[Candle] = None
        self.weekend_gaps: list[dict] = []
        self._active_gap_ids: set[str] = set()
        self._gap_seq = 0

    def update(self, candle: Candle) -> None:
        day = candle.timestamp.date()
        week = candle.timestamp.isocalendar()[:2]

        if self._current_day is not None and day != self._current_day:
            self.pdh = {"level_type": "PDH", "value": self._day_high, "reference_period_start": self._current_day, "available_from": candle.timestamp}
            self.pdl = {"level_type": "PDL", "value": self._day_low, "reference_period_start": self._current_day, "available_from": candle.timestamp}
            self.history.append(dict(self.pdh))
            self.history.append(dict(self.pdl))
            if self.event_bus:
                self.event_bus.publish(EventType.REFERENCE_LEVEL_UPDATED, candle.timestamp, dict(self.pdh))
                self.event_bus.publish(EventType.REFERENCE_LEVEL_UPDATED, candle.timestamp, dict(self.pdl))
            self._day_high, self._day_low = candle.high, candle.low
        else:
            self._day_high = candle.high if self._day_high is None else max(self._day_high, candle.high)
            self._day_low = candle.low if self._day_low is None else min(self._day_low, candle.low)
        self._current_day = day

        if self._current_week is not None and week != self._current_week:
            self.pwh = {"level_type": "PWH", "value": self._week_high, "reference_period_start": self._current_week, "available_from": candle.timestamp}
            self.pwl = {"level_type": "PWL", "value": self._week_low, "reference_period_start": self._current_week, "available_from": candle.timestamp}
            self.history.append(dict(self.pwh))
            self.history.append(dict(self.pwl))
            if self.event_bus:
                self.event_bus.publish(EventType.REFERENCE_LEVEL_UPDATED, candle.timestamp, dict(self.pwh))
                self.event_bus.publish(EventType.REFERENCE_LEVEL_UPDATED, candle.timestamp, dict(self.pwl))
            self._week_high, self._week_low = candle.high, candle.low
        else:
            self._week_high = candle.high if self._week_high is None else max(self._week_high, candle.high)
            self._week_low = candle.low if self._week_low is None else min(self._week_low, candle.low)
        self._current_week = week

        if self._prev_candle is not None:
            delta = candle.timestamp - self._prev_candle.timestamp
            if delta >= pd.Timedelta(hours=self.min_gap_hours) and self._prev_candle.timestamp.weekday() == 4:
                self._gap_seq += 1
                gap_size = candle.open - self._prev_candle.close
                gap = {
                    "gap_id": f"WEEKEND_GAP_{self._gap_seq}",
                    "friday_close_timestamp": self._prev_candle.timestamp, "friday_close": self._prev_candle.close,
                    "reopen_timestamp": candle.timestamp, "reopen_open": candle.open,
                    "gap_size": gap_size,
                    "gap_pct": (gap_size / self._prev_candle.close * 100.0) if self._prev_candle.close else 0.0,
                    "gap_direction": "up" if gap_size > 0 else ("down" if gap_size < 0 else "flat"),
                    "gap_filled_pct": 0.0, "gap_age_candles": 0, "state": "OPEN",
                    "_deepest": 0.0,
                }
                self.weekend_gaps.append(gap)
                self._active_gap_ids.add(gap["gap_id"])

        for gap_id in list(self._active_gap_ids):
            gap = next(g for g in self.weekend_gaps if g["gap_id"] == gap_id)
            gap["gap_age_candles"] += 1 if self._prev_candle is not None else 0
            size = abs(gap["gap_size"])
            if size > 0:
                if gap["gap_direction"] == "up":
                    depth = max(0.0, min(gap["reopen_open"] - candle.low, size))
                else:
                    depth = max(0.0, min(candle.high - gap["reopen_open"], size))
                gap["_deepest"] = max(gap["_deepest"], depth)
                gap["gap_filled_pct"] = round(100.0 * gap["_deepest"] / size, 2)
            else:
                gap["gap_filled_pct"] = 100.0
            if gap["gap_filled_pct"] >= 100.0:
                gap["state"] = "FILLED"
                self._active_gap_ids.discard(gap_id)
            elif gap["gap_filled_pct"] > 0.0:
                gap["state"] = "PARTIALLY_FILLED"

        self._prev_candle = candle

    def state_dict(self) -> dict:
        return {
            "current_day": str(self._current_day) if self._current_day else None,
            "day_high": self._day_high, "day_low": self._day_low,
            "current_week": list(self._current_week) if self._current_week else None,
            "week_high": self._week_high, "week_low": self._week_low,
            "pdh": self.pdh, "pdl": self.pdl, "pwh": self.pwh, "pwl": self.pwl,
            "history": self.history,
            "prev_candle": asdict(self._prev_candle) if self._prev_candle else None,
            "weekend_gaps": self.weekend_gaps, "active_gap_ids": list(self._active_gap_ids),
            "gap_seq": self._gap_seq,
        }

    def restore(self, state: dict) -> None:
        self._current_day = pd.Timestamp(state["current_day"]).date() if state["current_day"] else None
        self._day_high, self._day_low = state["day_high"], state["day_low"]
        self._current_week = tuple(state["current_week"]) if state["current_week"] else None
        self._week_high, self._week_low = state["week_high"], state["week_low"]
        self.pdh, self.pdl, self.pwh, self.pwl = state["pdh"], state["pdl"], state["pwh"], state["pwl"]
        self.history = state["history"]
        self._prev_candle = Candle(**state["prev_candle"]) if state["prev_candle"] else None
        self.weekend_gaps = state["weekend_gaps"]
        self._active_gap_ids = set(state["active_gap_ids"])
        self._gap_seq = state["gap_seq"]


# ---------------------------------------------------------------------------
# 9. Engulfing tracker (O(1) per candle -- only needs the previous candle)
# ---------------------------------------------------------------------------


class IncrementalEngulfingTracker:
    def __init__(self, config: EngulfingConfig):
        self.config = config
        self._prev: Optional[Candle] = None
        self.events: list[dict] = []
        self._seq = 0

    def update(self, candle: Candle) -> Optional[dict]:
        result = None
        if self._prev is not None:
            prev = self._prev
            direction = None
            if prev.direction == "bearish" and candle.direction == "bullish" and candle.open <= prev.close and candle.close >= prev.open:
                direction = "bullish"
            elif prev.direction == "bullish" and candle.direction == "bearish" and candle.open >= prev.close and candle.close <= prev.open:
                direction = "bearish"

            if direction:
                self._seq += 1
                engulfed_body = prev.body_size
                body_ratio = (candle.body_size / engulfed_body) if engulfed_body > 0 else float("inf")
                strength = "STRONG" if body_ratio >= self.config.strong_body_ratio else (
                    "NORMAL" if body_ratio >= self.config.normal_body_ratio else "WEAK"
                )
                result = {
                    "engulfing_id": f"ENGULF_{self._seq}", "direction": direction, "strength": strength,
                    "timestamp": candle.timestamp, "candle_index": candle.index,
                    "engulfed_candle_index": prev.index,
                    "body_ratio": round(body_ratio, 4) if body_ratio != float("inf") else None,
                }
                self.events.append(result)
        self._prev = candle
        return result

    def state_dict(self) -> dict:
        return {"prev": asdict(self._prev) if self._prev else None, "events": self.events, "seq": self._seq}

    def restore(self, state: dict) -> None:
        self._prev = Candle(**state["prev"]) if state["prev"] else None
        self.events = state["events"]
        self._seq = state["seq"]
