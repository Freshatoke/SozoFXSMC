"""
Task 11 Phase 2 — Live Data Provider architecture.

Deliberately mirrors `src.data.historical_pipeline`'s `DataAdapter`
Protocol + `ADAPTERS` registry + `get_adapter()` pattern (per the task
brief's explicit "design provider architecture identical to the
historical pipeline" instruction) -- same shape, adapted for a
streaming/polling data source instead of a one-shot file load:

    historical_pipeline.DataAdapter.load(path)      -> pd.DataFrame (batch)
    live.providers.base.LiveDataProvider.poll()      -> LiveCandleBatch (incremental)

Every provider must supply: connect/disconnect, a heartbeat, and
poll() -- the FeedManager (feed_manager.py) is what adds reconnect
policy, gap detection, and missing-candle recovery ON TOP of any
provider, so individual providers stay simple and swappable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol

import pandas as pd


class ProviderConnectionError(Exception):
    """Raised by a provider's connect()/poll() when the underlying
    source is unreachable -- FeedManager catches this specifically to
    drive reconnect/backoff logic, distinct from a programming error."""


@dataclass
class LiveCandleBatch:
    """One poll's worth of new, COMPLETED candles for one symbol.
    Never includes the still-forming current candle -- a live engine
    must only ever see a candle once its close time has passed, exactly
    the same anti-look-ahead discipline already enforced everywhere else
    in this platform."""
    symbol: str
    timeframe: str
    candles: pd.DataFrame   # columns: timestamp, open, high, low, close[, volume] -- always UTC
    provider_name: str
    polled_at: pd.Timestamp


@dataclass
class HeartbeatStatus:
    provider_name: str
    connected: bool
    last_successful_poll: Optional[pd.Timestamp] = None
    last_error: Optional[str] = None
    consecutive_failures: int = 0


class LiveDataProvider(Protocol):
    provider_name: str

    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def is_connected(self) -> bool: ...
    def poll(self, symbol: str, timeframe: str, since: Optional[pd.Timestamp]) -> LiveCandleBatch: ...
    def heartbeat(self) -> HeartbeatStatus: ...


PROVIDERS: dict = {}


def register_provider(cls):
    """Class decorator -- keeps the registry in sync with what's
    actually importable, the same self-registering pattern already used
    by src.data.historical_pipeline.ADAPTERS (there it's a manual dict;
    here a decorator avoids a second list to keep updated by hand)."""
    PROVIDERS[cls.provider_name] = cls
    return cls


def get_live_provider(name: str, **kwargs) -> LiveDataProvider:
    key = name.strip().lower()
    if key not in PROVIDERS:
        raise ValueError(f"Unsupported live provider: {name}. Registered: {sorted(PROVIDERS)}")
    return PROVIDERS[key](**kwargs)
