"""
Trade lifecycle model.

    Signal Generated -> Entry Validation -> Trade Open -> Trade Management
    -> Trade Close -> Trade Result -> Performance Recording

A `Trade` is created for every signal fed to the backtest engine, even if
it never actually enters the market -- an EXPIRED trade (entry condition
never triggered within `EntryConfig.max_wait_candles`) or a REJECTED trade
(a risk-management gate blocked it, e.g. max simultaneous positions) is
still recorded, with its status and the reason, so no signal silently
disappears from the historical record.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

import pandas as pd


class TradeStatus(str, Enum):
    PENDING = "PENDING"        # signal received, waiting for entry trigger
    OPEN = "OPEN"               # entered, being managed candle by candle
    CLOSED = "CLOSED"           # fully exited
    EXPIRED = "EXPIRED"         # entry never triggered within max_wait_candles
    REJECTED = "REJECTED"       # blocked by a risk-management gate before entry


class ExitReason(str, Enum):
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    TRAILING_STOP = "TRAILING_STOP"
    BREAKEVEN_STOP = "BREAKEVEN_STOP"
    PARTIAL_TAKE_PROFIT = "PARTIAL_TAKE_PROFIT"
    MAX_DURATION = "MAX_DURATION"
    SESSION_CLOSE = "SESSION_CLOSE"
    END_OF_DATA = "END_OF_DATA"


@dataclass
class ManagementEvent:
    timestamp: pd.Timestamp
    event_type: str          # "BREAKEVEN_MOVED" | "TRAILING_STOP_MOVED" | "PARTIAL_EXIT" | ...
    detail: dict = field(default_factory=dict)


@dataclass
class Trade:
    trade_id: str
    signal_id: str
    strategy_id: str
    symbol: str
    timeframe: str
    direction: str

    signal_timestamp: pd.Timestamp
    confidence_score: float
    reason_codes: list
    confluence_snapshot: dict

    entry_method: str
    stop_method: str
    take_profit_method: str

    status: str = TradeStatus.PENDING.value

    entry_price: Optional[float] = None
    entry_timestamp: Optional[pd.Timestamp] = None

    initial_stop_loss: Optional[float] = None
    current_stop_loss: Optional[float] = None
    take_profit_levels: list = field(default_factory=list)   # [(price, fraction_of_position), ...]

    position_size: Optional[float] = None       # lots
    risk_amount: Optional[float] = None         # account-currency risk at entry

    exit_price: Optional[float] = None
    exit_timestamp: Optional[pd.Timestamp] = None
    exit_reason: Optional[str] = None

    realized_pnl: float = 0.0
    commission_paid: float = 0.0
    slippage_cost: float = 0.0

    remaining_fraction: float = 1.0
    partial_exits: list = field(default_factory=list)   # [{"timestamp","price","fraction","pnl"}, ...]
    management_events: list = field(default_factory=list)

    mae: float = 0.0
    mfe: float = 0.0
    duration_candles: int = 0

    r_multiple: Optional[float] = None
    rejection_reason: Optional[str] = None

    session: Optional[str] = None
    metadata: dict = field(default_factory=dict)

    def add_management_event(self, timestamp, event_type: str, **detail) -> None:
        self.management_events.append(ManagementEvent(timestamp=timestamp, event_type=event_type, detail=detail))

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["take_profit_levels"] = [list(x) for x in d["take_profit_levels"]]
        d["management_events"] = [
            {"timestamp": e.timestamp, "event_type": e.event_type, "detail": e.detail} for e in d["management_events"]
        ]
        return d
