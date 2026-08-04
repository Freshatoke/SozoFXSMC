"""
Lightweight synchronous in-process event bus.

Strategies (future tasks) subscribe to the event types they care about
instead of polling every feature-engine module on every candle. This bus
does not do anything clever (no async, no persistence, no networking) --
it is a plain publish/subscribe registry plus an append-only event log,
which is what makes "no duplicated events" / "no missing events" /
"event ordering" testable (tests/test_incremental_engine.py).

Event ordering guarantee: events published during the processing of a
single candle are appended to the log, and delivered to subscribers, in
the exact order `IncrementalEngine.process_candle` emits them (see the
pipeline order documented there and in docs/CONFLUENCE_ENGINE.md). Events
are never reordered or batched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class EventType(str, Enum):
    SWING_CONFIRMED = "SwingConfirmed"
    BULLISH_BOS = "BullishBOS"
    BEARISH_BOS = "BearishBOS"
    BULLISH_CHOCH = "BullishCHoCH"
    BEARISH_CHOCH = "BearishCHoCH"
    ORDER_BLOCK_CREATED = "OrderBlockCreated"
    ORDER_BLOCK_MITIGATED = "OrderBlockMitigated"
    ORDER_BLOCK_INVALIDATED = "OrderBlockInvalidated"
    FVG_CREATED = "FVGCreated"
    FVG_MITIGATED = "FVGMitigated"
    LIQUIDITY_CREATED = "LiquidityCreated"
    LIQUIDITY_SWEPT = "LiquiditySwept"
    SESSION_STARTED = "SessionStarted"
    SESSION_ENDED = "SessionEnded"
    REFERENCE_LEVEL_UPDATED = "ReferenceLevelUpdated"
    CONFLUENCE_UPDATED = "ConfluenceUpdated"


@dataclass(frozen=True)
class Event:
    event_type: EventType
    timestamp: Any            # pandas.Timestamp of the candle that produced this event
    payload: dict = field(default_factory=dict)
    sequence: int = 0          # monotonically increasing, assigned by the bus at publish time


class EventBus:
    def __init__(self):
        self._subscribers: dict[EventType, list[Callable[[Event], None]]] = {}
        self._log: list[Event] = []
        self._seq = 0

    def subscribe(self, event_type: EventType, callback: Callable[[Event], None]) -> None:
        self._subscribers.setdefault(event_type, []).append(callback)

    def publish(self, event_type: EventType, timestamp: Any, payload: dict | None = None) -> Event:
        self._seq += 1
        event = Event(event_type=event_type, timestamp=timestamp, payload=payload or {}, sequence=self._seq)
        self._log.append(event)
        for callback in self._subscribers.get(event_type, []):
            callback(event)
        return event

    @property
    def log(self) -> list[Event]:
        return list(self._log)

    def events_of_type(self, event_type: EventType) -> list[Event]:
        return [e for e in self._log if e.event_type == event_type]

    def clear_log(self) -> None:
        self._log.clear()
