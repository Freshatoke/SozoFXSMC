"""
Task 11 Phase 2 — MT5 live provider.

Interface-complete, NOT connectable in this environment: MetaTrader 5
requires a running MT5 terminal (via the `MetaTrader5` Python package,
which talks to a local terminal process over IPC) plus real account
login credentials, neither of which exist here. Per the explicit choice
made for this task, this adapter is honest about that rather than
faking a connection -- `connect()` raises `ProviderConnectionError`
with a clear message about what's actually required, exactly the same
"tell the truth about what's not done" principle used throughout this
platform's documentation (e.g. the Task 10 Production Readiness
Checklist's explicit NOT DONE items).

To make this real: `pip install MetaTrader5`, run it on Windows with a
real MT5 terminal installed and logged in, and replace the body of
`connect()`/`poll()` with `MetaTrader5.initialize(...)` /
`MetaTrader5.copy_rates_from(...)` calls. The rest of this platform
(FeedManager, IncrementalMarketContext, strategies, decision engine)
needs zero changes to consume it -- it only depends on the
`LiveDataProvider` protocol.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from src.live.providers.base import (
    LiveDataProvider, LiveCandleBatch, HeartbeatStatus, ProviderConnectionError, register_provider,
)


@register_provider
class MT5LiveProvider:
    provider_name = "mt5"

    def __init__(self, login: Optional[int] = None, password: Optional[str] = None, server: Optional[str] = None, terminal_path: Optional[str] = None):
        self.login = login
        self.password = password
        self.server = server
        self.terminal_path = terminal_path
        self._connected = False
        self._last_error: Optional[str] = None

    def connect(self) -> None:
        try:
            import MetaTrader5  # noqa: F401
        except ImportError as exc:
            self._last_error = "MetaTrader5 package not installed"
            raise ProviderConnectionError(
                "MT5 live provider requires the 'MetaTrader5' package and a running MT5 terminal "
                "with valid login credentials -- none are available in this environment. "
                "Install MetaTrader5, supply login/password/server, and this adapter is ready to use."
            ) from exc
        if not (self.login and self.password and self.server):
            raise ProviderConnectionError("MT5 login/password/server not supplied -- cannot connect without real credentials.")
        # Real implementation (when credentials + terminal are available):
        #   ok = MetaTrader5.initialize(path=self.terminal_path, login=self.login, password=self.password, server=self.server)
        #   if not ok: raise ProviderConnectionError(MetaTrader5.last_error())
        #   self._connected = True
        raise ProviderConnectionError("MT5 terminal connection not implemented in this environment (no terminal available to test against).")

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def poll(self, symbol: str, timeframe: str, since: Optional[pd.Timestamp]) -> LiveCandleBatch:
        raise ProviderConnectionError("MT5 provider is not connected -- see connect() for requirements.")

    def heartbeat(self) -> HeartbeatStatus:
        return HeartbeatStatus(provider_name=self.provider_name, connected=self._connected, last_error=self._last_error)
