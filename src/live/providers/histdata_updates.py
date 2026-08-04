"""
Task 11 Phase 2 — HistData "updates" provider.

HistData.com is a periodic archive publisher (monthly ZIP drops), not a
streaming/real-time API -- there is no live socket or REST endpoint to
poll. The honest, functional interpretation of "support HistData
updates" is a DIRECTORY WATCHER: periodically scan a configured
"incoming" folder for newly-arrived HistData exports (the same format
Task 7.3's `HistDataAdapter`/`load_histdata_zip` already parses) and
import any not seen before -- exactly what an operator would do when a
new monthly HistData file is manually downloaded and dropped in.

This reuses `src.data.providers.histdata.HistDataAdapter` for parsing
rather than duplicating it -- this module only adds the "what's new
since last time" polling loop around it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pandas as pd

from src.data.providers.histdata import HistDataAdapter
from src.live.providers.base import (
    LiveDataProvider, LiveCandleBatch, HeartbeatStatus, ProviderConnectionError, register_provider,
)


@register_provider
class HistDataUpdatesProvider:
    provider_name = "histdata_updates"

    def __init__(self, incoming_dir: str = "data/live/histdata_incoming"):
        self.incoming_dir = Path(incoming_dir)
        self.adapter = HistDataAdapter()
        self._seen_files: set[str] = set()
        self._connected = False
        self._last_successful_poll: Optional[pd.Timestamp] = None
        self._last_error: Optional[str] = None

    def connect(self) -> None:
        self.incoming_dir.mkdir(parents=True, exist_ok=True)
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def poll(self, symbol: str, timeframe: str, since: Optional[pd.Timestamp]) -> LiveCandleBatch:
        if not self._connected:
            raise ProviderConnectionError("HistDataUpdatesProvider not connected -- call connect() first.")
        try:
            candidates = sorted(self.incoming_dir.glob(f"{symbol}*.zip")) + sorted(self.incoming_dir.glob(f"{symbol}*.csv"))
            new_files = [p for p in candidates if str(p) not in self._seen_files]
            frames = []
            for path in new_files:
                df = self.adapter.load(path)
                frames.append(df)
                self._seen_files.add(str(path))
            if frames:
                combined = pd.concat(frames, ignore_index=True).sort_values("timestamp").reset_index(drop=True)
                if since is not None:
                    combined = combined[combined["timestamp"] > since].reset_index(drop=True)
            else:
                combined = pd.DataFrame(columns=["timestamp", "open", "high", "low", "close"])
        except Exception as exc:
            self._last_error = str(exc)
            raise ProviderConnectionError(f"HistData update scan failed: {exc}") from exc

        self._last_successful_poll = pd.Timestamp.now(tz="UTC")
        return LiveCandleBatch(symbol=symbol, timeframe=timeframe, candles=combined, provider_name=self.provider_name, polled_at=self._last_successful_poll)

    def heartbeat(self) -> HeartbeatStatus:
        return HeartbeatStatus(provider_name=self.provider_name, connected=self._connected, last_successful_poll=self._last_successful_poll, last_error=self._last_error)
