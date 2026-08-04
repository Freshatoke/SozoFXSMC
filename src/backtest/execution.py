"""
Execution Model: turns theoretical trigger prices (from entry/stop_loss/
take_profit) into realistic filled prices and account-currency cash
flows. No method here assumes a perfect fill.

Spread convention (a standard, simplified round-trip model): the OHLC
series is treated as the MID price. The full spread is charged once, at
entry (buy at ask = mid + spread; sell-entry at bid = mid - spread);
exits are filled at mid (i.e. the opposing side of the spread the trader
would receive, which nets out to "spread charged once per round trip").

Slippage is applied unfavourably at ENTRY and at STOP-LOSS exits only --
never at take-profit fills (a take-profit is a resting limit order that
fills at its price; a stop-loss is a market order triggered by an adverse
move, which realistically slips).

Latency (`config.latency_candles`) is not applied here -- it shifts WHEN
a trigger is sampled, which is the entry/stop/TP engines' concern (see
`src.backtest.engine`); this module only concerns itself with price
adjustment and cash flow once a trigger candle is chosen.
"""

from __future__ import annotations

from config.settings import ExecutionConfig


def apply_spread_at_entry(raw_price: float, direction: str, config: ExecutionConfig) -> float:
    spread = config.spread_pips * config.pip_size
    return raw_price + spread if direction == "bullish" else raw_price - spread


def apply_slippage(raw_price: float, direction: str, is_adverse_exit: bool, config: ExecutionConfig) -> float:
    """`is_adverse_exit=True` for stop-loss/trailing-stop exits (and entry
    fills); take-profit exits never slip."""
    if not is_adverse_exit:
        return raw_price
    slip = config.slippage_pips * config.pip_size
    return raw_price + slip if direction == "bullish" else raw_price - slip


def fill_entry(raw_price: float, direction: str, config: ExecutionConfig) -> float:
    price = apply_spread_at_entry(raw_price, direction, config)
    price = apply_slippage(price, direction, is_adverse_exit=True, config=config)
    return price


def fill_exit(raw_price: float, direction: str, is_stop_exit: bool, config: ExecutionConfig) -> float:
    """`direction` here is the TRADE's direction; a stop/adverse exit on a
    bullish trade slips further down (worse), on a bearish trade slips
    further up (worse)."""
    if not is_stop_exit:
        return raw_price
    slip = config.slippage_pips * config.pip_size
    return raw_price - slip if direction == "bullish" else raw_price + slip


def compute_commission(lots: float, config: ExecutionConfig) -> float:
    return lots * config.commission_per_lot


def compute_pnl(direction: str, entry_price: float, exit_price: float, lots: float, config: ExecutionConfig) -> float:
    diff = (exit_price - entry_price) if direction == "bullish" else (entry_price - exit_price)
    return diff * config.contract_size * lots


def pip_value_per_lot(config: ExecutionConfig) -> float:
    return config.pip_size * config.contract_size
