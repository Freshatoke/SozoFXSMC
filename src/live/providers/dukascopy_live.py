"""
Task 11 Phase 2 — Dukascopy "near-live" polling provider.

Dukascopy publishes its tick archive with a short lag (not sub-second
streaming) -- this provider polls the ALREADY-VERIFIED-REACHABLE
Dukascopy tick endpoint (`src.data.providers.dukascopy`, the same one
Task 8's historical downloader uses, confirmed reachable from this
environment) for the most recently completed UTC hour(s) and aggregates
ticks to M1 candles via the existing, already-tested `ticks_to_m1`.

This is a REAL external network connection, not a simulation -- but it
is polling-based (once per call), not a persistent streaming socket,
because Dukascopy's public archive doesn't offer one. That distinction
is documented, not hidden.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd

from src.data.providers.dukascopy import (
    DukascopyDownloader, DukascopyDownloadConfig, parse_tick_bi5, ticks_to_m1, cache_path,
)
from src.live.providers.base import (
    LiveDataProvider, LiveCandleBatch, HeartbeatStatus, ProviderConnectionError, register_provider,
)


@register_provider
class DukascopyLiveProvider:
    provider_name = "dukascopy_live"

    def __init__(self, cache_location: str = "data/live/dukascopy_cache", lookback_hours: int = 2, timeout: int = 15, retries: int = 2):
        self.config = DukascopyDownloadConfig(cache_location=cache_location)
        self.downloader = DukascopyDownloader(config=self.config)
        self.lookback_hours = lookback_hours
        self.timeout = timeout
        self.retries = retries
        self._connected = False
        self._last_successful_poll: Optional[pd.Timestamp] = None
        self._last_error: Optional[str] = None
        self._consecutive_failures = 0

    def connect(self) -> None:
        # Dukascopy's archive is stateless HTTP -- "connecting" here means
        # verifying the endpoint is actually reachable right now (a cheap
        # single-hour probe), the same fail-fast principle a real socket
        # connection would need, before the feed manager starts relying on it.
        probe_hour = (datetime.now(timezone.utc) - timedelta(hours=self.lookback_hours)).replace(minute=0, second=0, microsecond=0)
        try:
            path = self.downloader.download_hour("EURUSD", probe_hour.date(), probe_hour.hour, retries=1, timeout=self.timeout)
            self._connected = path is not None or True  # a 404 (no data that hour, e.g. weekend) is not a connectivity failure
        except Exception as exc:
            self._connected = False
            self._last_error = str(exc)
            raise ProviderConnectionError(f"Dukascopy probe failed: {exc}") from exc

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def poll(self, symbol: str, timeframe: str, since: Optional[pd.Timestamp]) -> LiveCandleBatch:
        if timeframe != "M1":
            raise ValueError("DukascopyLiveProvider only supports M1 (aggregate downstream for M5/M15, matching src.data.resample's existing convention)")

        now = datetime.now(timezone.utc)
        # Only hours that have fully elapsed are polled -- the current,
        # still-in-progress hour is never fetched (its .bi5 file would be
        # incomplete), matching the platform-wide "never touch an
        # unclosed candle" rule.
        latest_complete_hour = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
        earliest_hour = latest_complete_hour - timedelta(hours=self.lookback_hours - 1)
        if since is not None:
            since_hour = pd.Timestamp(since).to_pydatetime().replace(minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
            earliest_hour = max(earliest_hour, since_hour)

        # Task 11 Phase 2 resilience note: a single hour's transient
        # network failure (timeout, temporary DNS hiccup) must not abort
        # the whole poll -- that hour is simply missing from this batch,
        # which FeedManager's gap detection (feed_manager.py) will notice
        # and attempt to recover on a later poll. Only raise
        # ProviderConnectionError if EVERY hour in the window failed
        # (a real, total connectivity problem, not routine flakiness).
        frames = []
        hour_cursor = earliest_hour
        hours_attempted = hours_failed = 0
        while hour_cursor <= latest_complete_hour:
            hours_attempted += 1
            try:
                path = self.downloader.download_hour(symbol, hour_cursor.date(), hour_cursor.hour, retries=self.retries, timeout=self.timeout)
                if path is not None:
                    ticks = parse_tick_bi5(path, symbol, hour_cursor.date(), hour_cursor.hour)
                    if not ticks.empty:
                        frames.append(ticks)
            except Exception as exc:
                hours_failed += 1
                self._last_error = str(exc)
            hour_cursor += timedelta(hours=1)

        if hours_attempted > 0 and hours_failed == hours_attempted:
            self._consecutive_failures += 1
            raise ProviderConnectionError(f"Dukascopy poll failed for {symbol}: every hour in the window errored, last error: {self._last_error}")

        if not frames:
            candles = pd.DataFrame(columns=["timestamp", "open", "high", "low", "close"])
        else:
            all_ticks = pd.concat(frames, ignore_index=True).sort_values("timestamp").reset_index(drop=True)
            candles = ticks_to_m1(all_ticks, symbol)
            if since is not None and not candles.empty:
                candles = candles[candles["timestamp"] > since].reset_index(drop=True)

        self._last_successful_poll = pd.Timestamp.now(tz="UTC")
        self._consecutive_failures = 0
        return LiveCandleBatch(symbol=symbol, timeframe=timeframe, candles=candles, provider_name=self.provider_name, polled_at=self._last_successful_poll)

    def heartbeat(self) -> HeartbeatStatus:
        return HeartbeatStatus(
            provider_name=self.provider_name, connected=self._connected,
            last_successful_poll=self._last_successful_poll, last_error=self._last_error,
            consecutive_failures=self._consecutive_failures,
        )
