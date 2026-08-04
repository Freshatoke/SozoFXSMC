"""
Active Object Registry.

The single place future strategies look for "what does the market look
like right now" -- it never recomputes anything itself; it just mirrors
the current state held by the incremental trackers (src.engine.incremental)
after each candle's pipeline run (src.engine.engine.IncrementalEngine).

Objects are NEVER removed from the registry's underlying lists when they
become mitigated/invalidated/archived -- only their `state`/`current_state`
field changes (see each tracker's docstring for its exact state machine,
which is unchanged from docs/SMC_FEATURE_ENGINE.md). `active_*` accessors
filter for the subset still relevant to a strategy; the full history is
always available via `all_order_blocks`, `all_fvgs`, etc.

Persistence: `to_dict()` / `from_dict()` (and the `save`/`load` JSON
wrappers) serialize every tracker's internal state, so a process can be
restarted and resume exactly where it left off -- required for live
trading and long-running research per the task brief. See
docs/CONFLUENCE_ENGINE.md "Recovery mechanism".
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import pandas as pd


def _json_default(obj):
    if isinstance(obj, (pd.Timestamp,)):
        return {"__ts__": obj.isoformat()}
    if hasattr(obj, "isoformat"):
        return {"__date__": obj.isoformat()}
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def _json_object_hook(d):
    if "__ts__" in d:
        return pd.Timestamp(d["__ts__"])
    if "__date__" in d:
        return pd.Timestamp(d["__date__"]).date()
    return d


@dataclass
class ConfluenceSnapshot:
    timestamp: Any
    symbol: str
    timeframe: str
    market_state: str
    trend: str
    active_order_blocks: list
    active_fvgs: list
    active_liquidity: list
    current_session: dict
    asian_high: Optional[float]
    asian_low: Optional[float]
    pdh: Optional[float]
    pdl: Optional[float]
    weekend_gap: Optional[dict]
    engulfing_signal: Optional[dict]
    displacement_signal: Optional[dict]

    def to_dict(self) -> dict:
        """Deliberately a SHALLOW copy (`vars`), not `dataclasses.asdict`.

        `asdict` recursively deep-copies every nested value, and falls
        back to `copy.deepcopy` for anything that isn't a dataclass/list/
        dict/tuple -- including every `pd.Timestamp` in every active
        Order Block/FVG/liquidity dict. Called once per candle, that
        turned an O(1)-ish snapshot build into the dominant cost of the
        whole pipeline (profiled: ~90% of total runtime on a 2k-candle
        stream). A shallow copy is correct here because every list this
        snapshot holds (`active_order_blocks` etc.) is already a fresh,
        non-shared copy produced by the tracker accessors when this
        snapshot was built (see IncrementalOrderBlockTracker.active_order_blocks
        and its siblings) -- nothing mutates them afterward, so there is
        nothing for a deep copy to protect against that the shallow copy
        doesn't already guarantee.
        """
        return dict(vars(self))


class ActiveObjectRegistry:
    """Read-mostly view over the incremental trackers' current state.

    `refresh()` is called once per candle by IncrementalEngine after every
    tracker has processed that candle -- it is an O(active objects)
    operation (list comprehensions over currently-active items only), not
    an O(history) recomputation.
    """

    def __init__(self, symbol: str, timeframe: str):
        self.symbol = symbol
        self.timeframe = timeframe

        self.active_swing_highs: list[dict] = []
        self.active_swing_lows: list[dict] = []
        self.active_bullish_order_blocks: list[dict] = []
        self.active_bearish_order_blocks: list[dict] = []
        self.active_bullish_fvgs: list[dict] = []
        self.active_bearish_fvgs: list[dict] = []
        self.active_liquidity_levels: list[dict] = []
        self.active_sessions: dict[str, Optional[dict]] = {}
        self.reference_levels: dict[str, Optional[dict]] = {"PDH": None, "PDL": None, "PWH": None, "PWL": None}
        self.market_structure_state: str = "UNKNOWN"
        self.trend_state: str = "UNKNOWN"
        self.current_confluence_snapshot: Optional[ConfluenceSnapshot] = None

    def refresh(
        self,
        swing_tracker, structure_tracker, ob_tracker, fvg_tracker,
        liquidity_tracker, session_tracker, reference_tracker,
    ) -> None:
        all_swings = swing_tracker.confirmed_swings
        self.active_swing_highs = [s for s in all_swings if s["swing_type"] == "high"][-50:]
        self.active_swing_lows = [s for s in all_swings if s["swing_type"] == "low"][-50:]

        active_obs = ob_tracker.active_order_blocks()
        self.active_bullish_order_blocks = [ob for ob in active_obs if ob["direction"] == "bullish"]
        self.active_bearish_order_blocks = [ob for ob in active_obs if ob["direction"] == "bearish"]

        active_fvgs = fvg_tracker.active_fvgs()
        self.active_bullish_fvgs = [f for f in active_fvgs if f["direction"] == "bullish"]
        self.active_bearish_fvgs = [f for f in active_fvgs if f["direction"] == "bearish"]

        self.active_liquidity_levels = liquidity_tracker.active_levels()
        self.active_sessions = session_tracker.current_sessions()
        self.reference_levels = {
            "PDH": reference_tracker.pdh, "PDL": reference_tracker.pdl,
            "PWH": reference_tracker.pwh, "PWL": reference_tracker.pwl,
        }
        self.market_structure_state = structure_tracker.state
        self.trend_state = structure_tracker.state

    def build_confluence_snapshot(
        self, timestamp, engulfing_signal: Optional[dict], displacement_signal: Optional[dict],
        open_weekend_gap: Optional[dict],
    ) -> ConfluenceSnapshot:
        tokyo = self.active_sessions.get("tokyo")
        snapshot = ConfluenceSnapshot(
            timestamp=timestamp,
            symbol=self.symbol,
            timeframe=self.timeframe,
            market_state=self.market_structure_state,
            trend=self.trend_state,
            active_order_blocks=self.active_bullish_order_blocks + self.active_bearish_order_blocks,
            active_fvgs=self.active_bullish_fvgs + self.active_bearish_fvgs,
            active_liquidity=self.active_liquidity_levels,
            current_session={k: (v["session_name"] if v else None) for k, v in self.active_sessions.items() if v},
            asian_high=tokyo["high"] if tokyo else None,
            asian_low=tokyo["low"] if tokyo else None,
            pdh=self.reference_levels["PDH"]["value"] if self.reference_levels["PDH"] else None,
            pdl=self.reference_levels["PDL"]["value"] if self.reference_levels["PDL"] else None,
            weekend_gap=open_weekend_gap,
            engulfing_signal=engulfing_signal,
            displacement_signal=displacement_signal,
        )
        self.current_confluence_snapshot = snapshot
        return snapshot

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol, "timeframe": self.timeframe,
            "active_swing_highs": self.active_swing_highs, "active_swing_lows": self.active_swing_lows,
            "active_bullish_order_blocks": self.active_bullish_order_blocks,
            "active_bearish_order_blocks": self.active_bearish_order_blocks,
            "active_bullish_fvgs": self.active_bullish_fvgs, "active_bearish_fvgs": self.active_bearish_fvgs,
            "active_liquidity_levels": self.active_liquidity_levels,
            "market_structure_state": self.market_structure_state, "trend_state": self.trend_state,
            "current_confluence_snapshot": self.current_confluence_snapshot.to_dict() if self.current_confluence_snapshot else None,
        }

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), default=_json_default, indent=2))

    @classmethod
    def load_summary(cls, path: str | Path) -> dict:
        """Loads the plain summary dict (registry view only). Full engine
        state restoration goes through IncrementalEngine.load (see
        src/engine/engine.py), which also restores each tracker's
        internal buffers so processing can resume seamlessly."""
        p = Path(path)
        return json.loads(p.read_text(), object_hook=_json_object_hook)
