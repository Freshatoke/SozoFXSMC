"""
Task 11 Phase 6 — Paper Trading Broker.

A simulated broker that turns EXECUTE decisions (Phase 5) into positions
and, candle by candle, decides fills exactly the way `src.backtest`
already does for historical replay -- spread/slippage/commission via
`src.backtest.execution` (Task 4, already validated across Tasks 7-9's
whole research programme) and breakeven/trailing-stop via
`src.backtest.management`'s pure functions, reused here (not
reimplemented) by giving `PaperPosition` the same attribute names
(`direction`, `entry_price`, `initial_stop_loss`, `current_stop_loss`)
those functions already expect.

No real broker connection of any kind -- this never sends an order
anywhere. Support: market orders, pending (limit/stop) orders, SL, TP,
partial exits, breakeven, trailing stop, slippage, spread, commission,
margin, swap.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from config.settings import ExecutionConfig, ManagementConfig
from src.backtest.execution import fill_entry, fill_exit, compute_commission, compute_pnl
from src.backtest.management import check_breakeven, check_trailing_stop

_position_id_seq = itertools.count(1)
_order_id_seq = itertools.count(1)


@dataclass
class PaperPosition:
    position_id: str
    symbol: str
    direction: str                       # "bullish" | "bearish"
    entry_price: float
    lots: float
    initial_stop_loss: float
    current_stop_loss: float
    take_profit_levels: list             # [(price, fraction_of_ORIGINAL_lots), ...], ascending distance from entry
    remaining_lots: float
    opened_at: pd.Timestamp
    strategy_id: str = ""
    status: str = "OPEN"                 # "OPEN" | "CLOSED"
    tp_levels_hit: set = field(default_factory=set)
    partial_exits: list = field(default_factory=list)   # [{"price", "lots", "pnl", "commission", "timestamp"}, ...]
    realized_pnl: float = 0.0
    total_commission: float = 0.0
    total_swap: float = 0.0
    closed_at: Optional[pd.Timestamp] = None
    exit_reason: Optional[str] = None
    _last_swap_date: Optional[object] = None


@dataclass
class PendingOrder:
    order_id: str
    symbol: str
    direction: str
    order_type: str                      # "limit" | "stop"
    trigger_price: float
    lots: float
    stop_loss: float
    take_profit_levels: list
    strategy_id: str = ""
    status: str = "PENDING"              # "PENDING" | "FILLED" | "CANCELLED"


class PaperBroker:
    """One broker instance per account (typically one per live session,
    shared across every symbol -- balance/margin are account-wide)."""

    def __init__(self, execution_config: ExecutionConfig = ExecutionConfig(),
                 management_config: ManagementConfig = ManagementConfig(), starting_balance: float = 10_000.0,
                 on_event=None):
        self.execution_config = execution_config
        self.management_config = management_config
        self.balance = starting_balance
        self.open_positions: dict = {}
        self.pending_orders: dict = {}
        self.closed_positions: list = []
        self.on_event = on_event or (lambda event_type, detail: None)

    def _emit(self, event_type: str, **detail) -> None:
        self.on_event(event_type, detail)

    # ------------------------------------------------------------------
    # Margin
    # ------------------------------------------------------------------
    def _used_margin(self) -> float:
        return sum(
            p.remaining_lots * self.execution_config.contract_size * p.entry_price / self.execution_config.leverage
            for p in self.open_positions.values()
        )

    def free_margin(self) -> float:
        return self.balance - self._used_margin()

    def equity(self, mark_prices: dict) -> float:
        """mark_prices: {symbol: current_price} for every open symbol.
        Unrealized PnL of every open position at current market price."""
        unrealized = 0.0
        for p in self.open_positions.values():
            price = mark_prices.get(p.symbol)
            if price is None:
                continue
            unrealized += compute_pnl(p.direction, p.entry_price, price, p.remaining_lots, self.execution_config)
        return self.balance + unrealized

    # ------------------------------------------------------------------
    # Opening positions
    # ------------------------------------------------------------------
    def open_market_position(self, symbol: str, direction: str, lots: float, raw_price: float,
                              stop_loss: float, take_profit_levels: list, timestamp: pd.Timestamp,
                              strategy_id: str = "", position_id: Optional[str] = None) -> Optional[PaperPosition]:
        entry_price = fill_entry(raw_price, direction, self.execution_config)
        required_margin = lots * self.execution_config.contract_size * entry_price / self.execution_config.leverage
        if required_margin > self.free_margin():
            self._emit("order_rejected_insufficient_margin", symbol=symbol, required_margin=required_margin, free_margin=self.free_margin())
            return None

        position = PaperPosition(
            position_id=position_id or f"PP{next(_position_id_seq)}", symbol=symbol, direction=direction,
            entry_price=entry_price, lots=lots, initial_stop_loss=stop_loss, current_stop_loss=stop_loss,
            take_profit_levels=list(take_profit_levels), remaining_lots=lots, opened_at=timestamp,
            strategy_id=strategy_id,
        )
        self.open_positions[position.position_id] = position
        self._emit("paper_trade_opened", position_id=position.position_id, symbol=symbol, direction=direction,
                    entry_price=entry_price, lots=lots, stop_loss=stop_loss, timestamp=str(timestamp))
        return position

    def place_pending_order(self, symbol: str, direction: str, order_type: str, trigger_price: float,
                             lots: float, stop_loss: float, take_profit_levels: list, strategy_id: str = "") -> PendingOrder:
        order = PendingOrder(
            order_id=f"PO{next(_order_id_seq)}", symbol=symbol, direction=direction, order_type=order_type,
            trigger_price=trigger_price, lots=lots, stop_loss=stop_loss,
            take_profit_levels=list(take_profit_levels), strategy_id=strategy_id,
        )
        self.pending_orders[order.order_id] = order
        self._emit("pending_order_placed", order_id=order.order_id, symbol=symbol, order_type=order_type, trigger_price=trigger_price)
        return order

    def cancel_pending_order(self, order_id: str) -> None:
        order = self.pending_orders.pop(order_id, None)
        if order is not None:
            order.status = "CANCELLED"
            self._emit("pending_order_cancelled", order_id=order_id)

    # ------------------------------------------------------------------
    # Per-candle processing -- market orders trigger, SL/TP checked,
    # breakeven/trailing updated, swap accrued. `high`/`low`/`close` are
    # this candle's OHLC for `symbol`; `m1_so_far` is only needed for the
    # ATR trailing method (same signature as src.backtest.management).
    # ------------------------------------------------------------------
    def on_candle(self, symbol: str, timestamp: pd.Timestamp, open_: float, high: float, low: float, close: float,
                  m1_so_far: Optional[pd.DataFrame] = None) -> None:
        self._process_pending_orders(symbol, timestamp, high, low)
        self._process_open_positions(symbol, timestamp, high, low, close, m1_so_far)

    def _process_pending_orders(self, symbol: str, timestamp: pd.Timestamp, high: float, low: float) -> None:
        for order_id in list(self.pending_orders.keys()):
            order = self.pending_orders[order_id]
            if order.symbol != symbol or order.status != "PENDING":
                continue
            triggered = False
            if order.order_type == "limit":
                triggered = (low <= order.trigger_price) if order.direction == "bullish" else (high >= order.trigger_price)
            elif order.order_type == "stop":
                triggered = (high >= order.trigger_price) if order.direction == "bullish" else (low <= order.trigger_price)
            if not triggered:
                continue
            del self.pending_orders[order_id]
            order.status = "FILLED"
            position = self.open_market_position(
                symbol, order.direction, order.lots, order.trigger_price, order.stop_loss,
                order.take_profit_levels, timestamp, strategy_id=order.strategy_id,
            )
            self._emit("pending_order_filled", order_id=order_id, position_id=position.position_id if position else None)

    def _accrue_swap(self, position: PaperPosition, timestamp: pd.Timestamp) -> None:
        day = timestamp.date() if hasattr(timestamp, "date") else timestamp
        if position._last_swap_date == day:
            return
        if position._last_swap_date is not None:
            per_lot = (self.execution_config.swap_long_per_lot_per_day if position.direction == "bullish"
                       else self.execution_config.swap_short_per_lot_per_day)
            swap = per_lot * position.remaining_lots
            position.total_swap += swap
            self.balance += swap
            self._emit("swap_accrued", position_id=position.position_id, amount=swap, timestamp=str(timestamp))
        position._last_swap_date = day

    def _close_lots(self, position: PaperPosition, lots: float, raw_exit_price: float, timestamp: pd.Timestamp,
                     is_stop_exit: bool, reason: str) -> float:
        exit_price = fill_exit(raw_exit_price, position.direction, is_stop_exit, self.execution_config)
        pnl = compute_pnl(position.direction, position.entry_price, exit_price, lots, self.execution_config)
        commission = compute_commission(lots, self.execution_config)
        net = pnl - commission
        position.remaining_lots = round(position.remaining_lots - lots, 8)
        position.realized_pnl += net
        position.total_commission += commission
        self.balance += net
        position.partial_exits.append({"price": exit_price, "lots": lots, "pnl": net, "commission": commission,
                                        "timestamp": timestamp, "reason": reason})
        self._emit("paper_trade_partial_exit" if position.remaining_lots > 1e-9 else "paper_trade_closed",
                    position_id=position.position_id, lots=lots, exit_price=exit_price, pnl=net, reason=reason,
                    remaining_lots=position.remaining_lots, timestamp=str(timestamp))
        return net

    def _process_open_positions(self, symbol: str, timestamp: pd.Timestamp, high: float, low: float, close: float,
                                 m1_so_far: Optional[pd.DataFrame]) -> None:
        for position_id in list(self.open_positions.keys()):
            position = self.open_positions[position_id]
            if position.symbol != symbol or position.status != "OPEN":
                continue

            self._accrue_swap(position, timestamp)

            # 1. Stop-loss (checked before TP -- a candle that spans both
            # the SL and a TP within its range is conservatively assumed
            # to have hit the adverse level first, same convention
            # src.backtest.engine uses for intrabar ambiguity).
            stop_hit = (low <= position.current_stop_loss) if position.direction == "bullish" else (high >= position.current_stop_loss)
            if stop_hit:
                self._close_lots(position, position.remaining_lots, position.current_stop_loss, timestamp, is_stop_exit=True, reason="StopLoss")
                self._finalize_if_flat(position, timestamp)
                continue

            # 2. Take-profit levels (partial exits), in order, only once each
            for idx, (level_price, fraction) in enumerate(position.take_profit_levels):
                if idx in position.tp_levels_hit:
                    continue
                hit = (high >= level_price) if position.direction == "bullish" else (low <= level_price)
                if not hit:
                    continue
                position.tp_levels_hit.add(idx)
                exit_lots = round(min(position.lots * fraction, position.remaining_lots), 8)
                if exit_lots <= 0:
                    continue
                self._close_lots(position, exit_lots, level_price, timestamp, is_stop_exit=False,
                                  reason=f"TakeProfit{idx + 1}")
                if position.remaining_lots <= 1e-9:
                    break

            if self._finalize_if_flat(position, timestamp):
                continue

            # 3. Breakeven / trailing stop (reusing Task 4's tested,
            # stateless management functions -- PaperPosition exposes the
            # same attribute names Trade does, so no adaptation needed).
            candle = {"high": high, "low": low, "close": close}
            be_price = check_breakeven(position, candle, self.management_config, self.execution_config.pip_size)
            if be_price is not None:
                position.current_stop_loss = be_price
                self._emit("breakeven_applied", position_id=position.position_id, new_stop=be_price, timestamp=str(timestamp))

            trail_price = check_trailing_stop(position, candle, m1_so_far if m1_so_far is not None else pd.DataFrame(),
                                               self.management_config, self.execution_config.pip_size)
            if trail_price is not None:
                position.current_stop_loss = trail_price
                self._emit("trailing_stop_updated", position_id=position.position_id, new_stop=trail_price, timestamp=str(timestamp))

    def _finalize_if_flat(self, position: PaperPosition, timestamp: pd.Timestamp) -> bool:
        if position.remaining_lots > 1e-9:
            return False
        position.status = "CLOSED"
        position.closed_at = timestamp
        position.exit_reason = position.partial_exits[-1]["reason"] if position.partial_exits else "Unknown"
        self.open_positions.pop(position.position_id, None)
        self.closed_positions.append(position)
        return True

    def close_position_at_market(self, position_id: str, raw_price: float, timestamp: pd.Timestamp, reason: str = "ManualClose") -> Optional[float]:
        position = self.open_positions.get(position_id)
        if position is None:
            return None
        net = self._close_lots(position, position.remaining_lots, raw_price, timestamp, is_stop_exit=False, reason=reason)
        self._finalize_if_flat(position, timestamp)
        return net
