"""
Trade Management: pure, stateless functions called once per candle by
`src.backtest.engine`'s simulation loop for every OPEN trade. Each
function only ever looks at the CURRENT candle and the trade's own
history up to and including it -- no forward-looking data.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from config.settings import ManagementConfig
from src.backtest.trade import Trade


def _current_favorable_excursion(trade: Trade, candle) -> float:
    if trade.direction == "bullish":
        return candle["high"] - trade.entry_price
    return trade.entry_price - candle["low"]


def check_breakeven(trade: Trade, candle, config: ManagementConfig, pip_size: float) -> Optional[float]:
    if config.breakeven_trigger_r is None:
        return None
    if trade.initial_stop_loss is None or trade.entry_price is None:
        return None
    risk_distance = abs(trade.entry_price - trade.initial_stop_loss)
    if risk_distance <= 0:
        return None

    already_at_or_beyond_breakeven = (
        trade.current_stop_loss is not None
        and (
            (trade.direction == "bullish" and trade.current_stop_loss >= trade.entry_price)
            or (trade.direction == "bearish" and trade.current_stop_loss <= trade.entry_price)
        )
    )
    if already_at_or_beyond_breakeven:
        return None

    favorable = _current_favorable_excursion(trade, candle)
    if favorable >= config.breakeven_trigger_r * risk_distance:
        buffer = config.breakeven_buffer_pips * pip_size
        return trade.entry_price + buffer if trade.direction == "bullish" else trade.entry_price - buffer
    return None


def check_trailing_stop(trade: Trade, candle, m1_so_far: pd.DataFrame, config: ManagementConfig, pip_size: float) -> Optional[float]:
    if config.trailing_method is None:
        return None

    if config.trailing_method == "fixed_pips":
        distance = config.trailing_fixed_pips * pip_size
        candidate = candle["high"] - distance if trade.direction == "bullish" else candle["low"] + distance
    elif config.trailing_method == "atr":
        history = m1_so_far.tail(15)
        if len(history) < 2:
            return None
        prev_close = history["close"].shift(1)
        tr = pd.concat([
            history["high"] - history["low"],
            (history["high"] - prev_close).abs(),
            (history["low"] - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr = float(tr.dropna().mean()) if not tr.dropna().empty else 0.0
        if atr <= 0:
            return None
        distance = atr * config.trailing_atr_multiplier
        candidate = candle["high"] - distance if trade.direction == "bullish" else candle["low"] + distance
    elif config.trailing_method == "structure":
        # Trail behind the current candle's own low/high as a simple
        # structural trail (a full swing-based trail would require
        # re-querying MarketContext.swings on every candle, which is not
        # worth the cost for this v1.0 implementation -- see docs's
        # "Known limitations").
        candidate = candle["low"] if trade.direction == "bullish" else candle["high"]
    else:
        raise ValueError(f"Unknown trailing method: {config.trailing_method}")

    current = trade.current_stop_loss if trade.current_stop_loss is not None else trade.initial_stop_loss
    if trade.direction == "bullish":
        return candidate if candidate > current else None
    return candidate if candidate < current else None


def check_max_duration(trade: Trade, config: ManagementConfig) -> bool:
    if config.max_trade_duration_candles is None:
        return False
    return trade.duration_candles >= config.max_trade_duration_candles


def check_session_close(trade: Trade, candle_timestamp: pd.Timestamp, context, config: ManagementConfig) -> bool:
    if config.session_close_exit is None or context is None:
        return False
    sessions = context.sessions
    todays = sessions[
        (sessions.session_name == config.session_close_exit)
        & (sessions.start_utc <= candle_timestamp) & (sessions.end_utc >= candle_timestamp)
    ]
    if todays.empty:
        return False
    end_utc = todays.iloc[-1]["end_utc"]
    return candle_timestamp >= end_utc


def daily_trade_limit_reached(entries_today: int, config: ManagementConfig) -> bool:
    if config.daily_trade_limit is None:
        return False
    return entries_today >= config.daily_trade_limit
