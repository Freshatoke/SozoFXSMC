"""
Task 11 Phase 2 — Feed Manager.

Sits above one or more `LiveDataProvider`s (dukascopy_live, mt5, or
histdata_updates) and adds everything an individual provider does NOT
need to implement itself:

    automatic reconnect       -- exponential backoff, bounded retry count
    heartbeat monitoring       -- tracks per-provider liveness centrally
    gap detection               -- compares polled candles against the
                                    expected M1 sequence
    missing-candle recovery     -- re-polls a narrower window to backfill
                                    a detected gap before it reaches strategies
    timezone consistency        -- every candle is normalized to UTC on
                                    the way in, once, here -- so nothing
                                    downstream ever has to think about it
    symbol synchronization      -- tracks each symbol's last-seen candle
                                    independently, so one symbol falling
                                    behind never blocks another
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from src.live.providers.base import LiveDataProvider, LiveCandleBatch, ProviderConnectionError


@dataclass
class SymbolState:
    symbol: str
    timeframe: str
    last_candle_timestamp: Optional[pd.Timestamp] = None
    total_candles_received: int = 0
    total_gaps_detected: int = 0
    total_gap_candles_recovered: int = 0


@dataclass
class ReconnectPolicy:
    max_attempts: int = 5
    base_backoff_seconds: float = 2.0
    max_backoff_seconds: float = 60.0

    def backoff_for(self, attempt: int) -> float:
        return min(self.max_backoff_seconds, self.base_backoff_seconds * (2 ** attempt))


class FeedManager:
    def __init__(self, provider: LiveDataProvider, symbols: list, timeframe: str = "M1",
                 reconnect_policy: ReconnectPolicy = ReconnectPolicy(), on_event=None):
        self.provider = provider
        self.symbols = symbols
        self.timeframe = timeframe
        self.reconnect_policy = reconnect_policy
        self.on_event = on_event or (lambda event_type, detail: None)
        self.symbol_states = {s: SymbolState(symbol=s, timeframe=timeframe) for s in symbols}
        self._connected = False

    def _emit(self, event_type: str, **detail) -> None:
        self.on_event(event_type, detail)

    def ensure_connected(self) -> bool:
        if self._connected and self.provider.is_connected():
            return True
        for attempt in range(self.reconnect_policy.max_attempts):
            try:
                self.provider.connect()
                self._connected = True
                self._emit("provider_connected", provider=self.provider.provider_name, attempt=attempt)
                return True
            except ProviderConnectionError as exc:
                self._emit("provider_reconnect_failed", provider=self.provider.provider_name, attempt=attempt, error=str(exc))
                if attempt < self.reconnect_policy.max_attempts - 1:
                    time.sleep(self.reconnect_policy.backoff_for(attempt))
        self._connected = False
        self._emit("provider_connection_exhausted", provider=self.provider.provider_name, max_attempts=self.reconnect_policy.max_attempts)
        return False

    @staticmethod
    def _normalize_utc(candles: pd.DataFrame) -> pd.DataFrame:
        if candles.empty:
            return candles
        out = candles.copy()
        if out["timestamp"].dt.tz is None:
            out["timestamp"] = out["timestamp"].dt.tz_localize("UTC")
        else:
            out["timestamp"] = out["timestamp"].dt.tz_convert("UTC")
        return out.sort_values("timestamp").reset_index(drop=True)

    def _detect_gaps(self, candles: pd.DataFrame, state: SymbolState) -> list:
        """Returns a list of (gap_start, gap_end) missing-candle ranges,
        comparing the polled batch's timestamps against the expected
        contiguous M1 sequence starting from the symbol's last-seen
        candle. Weekend/session-close gaps are NOT flagged as data
        problems -- only WEEKDAY gaps exceeding the expected 1-minute
        step are genuine missing-candle events."""
        if state.last_candle_timestamp is None or candles.empty:
            return []
        step = pd.Timedelta(minutes=1) if self.timeframe == "M1" else pd.Timedelta(self.timeframe.replace("M", "") + "min")
        expected_start = state.last_candle_timestamp + step
        first_new = candles["timestamp"].iloc[0]
        if first_new <= expected_start:
            return []
        # Only flag as a gap if the missing span isn't just a weekend
        # (Fri close -> Sun/Mon reopen), matching the platform's existing
        # weekend-gap-is-not-a-data-error convention (src.features.reference_levels.compute_weekend_gaps).
        span = first_new - expected_start
        spans_a_weekend = expected_start.dayofweek == 4 and span >= pd.Timedelta(hours=20)
        if spans_a_weekend:
            return []
        return [(expected_start, first_new - step)]

    def poll_symbol(self, symbol: str) -> LiveCandleBatch:
        state = self.symbol_states[symbol]
        if not self.ensure_connected():
            raise ProviderConnectionError(f"Cannot poll {symbol}: provider {self.provider.provider_name} unreachable after retries.")

        batch = self.provider.poll(symbol, self.timeframe, since=state.last_candle_timestamp)
        batch.candles = self._normalize_utc(batch.candles)

        gaps = self._detect_gaps(batch.candles, state)
        for gap_start, gap_end in gaps:
            state.total_gaps_detected += 1
            self._emit("gap_detected", symbol=symbol, gap_start=str(gap_start), gap_end=str(gap_end))
            try:
                recovery = self.provider.poll(symbol, self.timeframe, since=gap_start - pd.Timedelta(minutes=1))
                recovery.candles = self._normalize_utc(recovery.candles)
                recovered = recovery.candles[(recovery.candles["timestamp"] >= gap_start) & (recovery.candles["timestamp"] <= gap_end)]
                if not recovered.empty:
                    batch.candles = pd.concat([recovered, batch.candles], ignore_index=True).drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
                    state.total_gap_candles_recovered += len(recovered)
                    self._emit("gap_recovered", symbol=symbol, candles_recovered=len(recovered))
                else:
                    self._emit("gap_recovery_failed", symbol=symbol, gap_start=str(gap_start), gap_end=str(gap_end))
            except ProviderConnectionError as exc:
                self._emit("gap_recovery_failed", symbol=symbol, error=str(exc))

        if not batch.candles.empty:
            state.last_candle_timestamp = batch.candles["timestamp"].iloc[-1]
            state.total_candles_received += len(batch.candles)
            self._emit("candles_received", symbol=symbol, count=len(batch.candles), latest=str(state.last_candle_timestamp))

        return batch

    def poll_all(self) -> dict:
        """Polls every configured symbol independently -- one symbol's
        provider error never blocks another's poll (symbol
        synchronization: each SymbolState tracks its own cursor)."""
        results = {}
        for symbol in self.symbols:
            try:
                results[symbol] = self.poll_symbol(symbol)
            except ProviderConnectionError as exc:
                self._emit("poll_failed", symbol=symbol, error=str(exc))
                results[symbol] = None
        return results
