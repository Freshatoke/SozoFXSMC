"""
Task 11 Phase 7 — Event Logger.

One append-only, structured audit trail for the whole live platform.
Every module already speaks one of two event idioms:

    on_event callback:  (event_type: str, detail: dict) -> None
        -- FeedManager, LiveDecisionEngine, PaperBroker

    EventBus.subscribe: (EventType, callback(Event)) -> None
        -- IncrementalEngine's per-timeframe feature trackers (swings,
           structure/CHoCH, order blocks, FVGs, liquidity, sessions),
           via LiveMarketContext.engine_bus

`EventLogger.bind(source, obj)` wires the first idiom in one line;
`EventLogger.bind_event_bus(source, event_bus)` wires the second by
subscribing to every `EventType` value. Every event, from either idiom,
lands in the same JSONL file with a single global sequence number, so the
full chain in the task brief's own example (
Liquidity sweep detected -> CHoCH confirmed -> IOS calculated ->
Trade approved -> Paper trade opened -> TP1 reached -> Breakeven ->
Trade closed) reconstructs as one ordered stream even though it spans
five different modules. Nothing is ever discarded (Phase 10's explicit
requirement) -- this is write-only from the platform's perspective;
querying is a read over the file, never a mutation.
"""

from __future__ import annotations

import json
import itertools
from pathlib import Path
from typing import Any

import pandas as pd

from src.engine.event_bus import EventBus, EventType


def _json_default(obj: Any):
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    if isinstance(obj, (set,)):
        return list(obj)
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return str(obj)


class EventLogger:
    def __init__(self, log_path: str | Path):
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._seq = itertools.count(1)
        self._fh = open(self.log_path, "a", encoding="utf-8")

    def log(self, source: str, event_type: str, timestamp: Any, detail: dict) -> None:
        record = {
            "sequence": next(self._seq),
            "logged_at": pd.Timestamp.now(tz="UTC").isoformat(),
            "source": source,
            "event_type": event_type,
            "event_timestamp": timestamp.isoformat() if hasattr(timestamp, "isoformat") else timestamp,
            "detail": detail,
        }
        self._fh.write(json.dumps(record, default=_json_default) + "\n")
        self._fh.flush()

    def bind(self, source: str, obj) -> None:
        """Wires an `on_event`-callback-style source (FeedManager,
        LiveDecisionEngine, PaperBroker) so every event it raises is
        logged. `obj` must accept `on_event` either as a constructor
        kwarg already passed, or as a settable attribute -- callers
        should construct these objects with `on_event=logger.handler(source)`
        directly (simplest); `bind` exists for objects already built."""
        obj.on_event = self.handler(source)

    def handler(self, source: str):
        def _handle(event_type: str, detail: dict) -> None:
            self.log(source, event_type, detail.get("timestamp"), detail)
        return _handle

    def bind_event_bus(self, source: str, event_bus: EventBus) -> None:
        for event_type in EventType:
            event_bus.subscribe(event_type, self._make_bus_handler(source))

    def _make_bus_handler(self, source: str):
        def _handle(event) -> None:
            self.log(source, event.event_type.value, event.timestamp, dict(event.payload))
        return _handle

    def close(self) -> None:
        self._fh.close()

    # ------------------------------------------------------------------
    # Read-side helpers -- for Phase 8's dashboard / Phase 11's analytics
    # ------------------------------------------------------------------
    def read_all(self) -> list:
        if not self.log_path.exists():
            return []
        with open(self.log_path, "r", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def read_as_dataframe(self) -> pd.DataFrame:
        return pd.DataFrame(self.read_all())
