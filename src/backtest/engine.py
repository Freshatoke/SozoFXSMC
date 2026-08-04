"""
BacktestEngine: simulates the complete trade lifecycle for a list of
Task 3 signals against historical M1 data.

    Signal Generated -> Entry Validation -> Trade Open -> Trade Management
    -> Trade Close -> Trade Result -> Performance Recording

Per-candle evaluation order (once a trade is OPEN), chosen specifically
to avoid look-ahead:
    1. Check if THIS candle hits the stop-loss set by PRIOR candles.
    2. Check if THIS candle hits any remaining take-profit level.
       (If both stop and a TP could be hit in the same candle, the stop
       is assumed to hit first -- the conservative, standard backtesting
       assumption when intra-candle order is unknown from OHLC alone.)
    3. If still open, update MAE/MFE and duration.
    4. Evaluate breakeven/trailing-stop rules using THIS candle's own
       extreme -- this updates the stop for the NEXT candle's check in
       step 1, never for this same candle (using this candle's own high/
       low to move the stop and then immediately re-checking the SAME
       candle against the new stop would be a subtle look-ahead/optimism
       bias).
    5. Check max-duration / session-close forced exits.

No trade is ever silently dropped: every signal produces a Trade record
with a terminal status of CLOSED, EXPIRED, or REJECTED.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from config.settings import (
    EntryConfig, StopLossConfig, TakeProfitConfig, ExecutionConfig, RiskConfig, ManagementConfig,
    DEFAULT_ENTRY_CONFIG, DEFAULT_STOP_LOSS_CONFIG, DEFAULT_TAKE_PROFIT_CONFIG,
    DEFAULT_EXECUTION_CONFIG, DEFAULT_RISK_CONFIG, DEFAULT_MANAGEMENT_CONFIG,
)
from src.backtest.trade import Trade, TradeStatus, ExitReason
from src.backtest.entry import resolve_entry
from src.backtest.stop_loss import resolve_stop_loss, _true_range_atr
from src.backtest.take_profit import resolve_take_profit
from src.backtest.execution import fill_entry, fill_exit, compute_commission, compute_pnl
from src.backtest.risk import resolve_position_size, RiskTracker
from src.backtest.management import check_breakeven, check_trailing_stop, check_max_duration, check_session_close, daily_trade_limit_reached


def _apply_latency(entry_result: dict, m1: pd.DataFrame, latency_candles: int) -> Optional[dict]:
    if latency_candles <= 0:
        return entry_result
    # Task 7.4 PERFORMANCE NOTE: same O(n)-per-trade full-filter issue as
    # elsewhere in this module -- see the searchsorted note on `window`
    # below. m1 is sorted ascending by timestamp by construction.
    start = m1["timestamp"].searchsorted(entry_result["entry_timestamp"], side="right")
    if len(m1) - start < latency_candles:
        return None
    row = m1.iloc[start + latency_candles - 1]
    return {"entry_price": float(row["open"]), "entry_timestamp": row["timestamp"]}


def simulate_trade(
    signal, m1: pd.DataFrame, context, risk_tracker: RiskTracker,
    entry_config: EntryConfig, stop_config: StopLossConfig, tp_config: TakeProfitConfig,
    execution_config: ExecutionConfig, risk_config: RiskConfig, management_config: ManagementConfig,
) -> Trade:
    trade = Trade(
        trade_id=f"TRADE_{signal.signal_id}",
        signal_id=signal.signal_id, strategy_id=signal.strategy_id,
        symbol=signal.symbol, timeframe=signal.timeframe, direction=signal.direction,
        signal_timestamp=signal.timestamp, confidence_score=signal.confidence_score,
        reason_codes=signal.reason_codes, confluence_snapshot=signal.confluence_snapshot,
        entry_method=entry_config.method, stop_method=stop_config.method, take_profit_method=tp_config.method,
        session=signal.session, metadata={},
    )

    entry_result = resolve_entry(signal, m1, entry_config, execution_config.pip_size)
    if entry_result is not None:
        entry_result = _apply_latency(entry_result, m1, execution_config.latency_candles)
    if entry_result is None:
        trade.status = TradeStatus.EXPIRED.value
        return trade

    entry_price_raw, entry_timestamp = entry_result["entry_price"], entry_result["entry_timestamp"]

    stop_result = resolve_stop_loss(signal, entry_price_raw, entry_timestamp, m1, context, stop_config, execution_config.pip_size)
    stop_loss_price = stop_result["stop_loss"]
    stop_distance = abs(entry_price_raw - stop_loss_price)
    if stop_distance <= 0:
        trade.status = TradeStatus.REJECTED.value
        trade.rejection_reason = "ZeroOrInvalidStopDistance"
        return trade

    atr = _true_range_atr(m1, entry_timestamp, risk_config.atr_period)
    lots = resolve_position_size(risk_tracker.balance, stop_distance, risk_config, execution_config, atr=atr)
    risk_amount = stop_distance * execution_config.contract_size * lots

    allowed, reason = risk_tracker.can_open(entry_timestamp, risk_amount)
    if not allowed:
        trade.status = TradeStatus.REJECTED.value
        trade.rejection_reason = reason
        return trade

    entry_price = fill_entry(entry_price_raw, signal.direction, execution_config)
    take_profit_levels = resolve_take_profit(signal, entry_price_raw, stop_loss_price, entry_timestamp, m1, context, tp_config, execution_config.pip_size)

    trade.status = TradeStatus.OPEN.value
    trade.entry_price = entry_price
    trade.entry_timestamp = entry_timestamp
    trade.initial_stop_loss = stop_loss_price
    trade.current_stop_loss = stop_loss_price
    trade.take_profit_levels = list(take_profit_levels)
    trade.position_size = lots
    trade.risk_amount = risk_amount

    risk_tracker.register_open(risk_amount)

    remaining_lots = lots
    total_pnl = 0.0
    total_commission = 0.0
    last_stop_move_reason = "STOP_LOSS"

    # Task 7.4 PERFORMANCE NOTE: m1[m1["timestamp"] > entry_timestamp] was a
    # full boolean filter over the ENTIRE m1 frame, run once per trade --
    # for a multi-year dataset with thousands of trades this is O(n *
    # num_trades), the single largest remaining bottleneck found when the
    # full 6.5-year campaign ran for hours instead of tens of minutes (it
    # wasn't visible at the smaller sizes used during earlier profiling
    # passes, since n was much smaller there). m1 is sorted ascending by
    # timestamp by construction, so searchsorted finds the identical cutoff
    # position in O(log n); positional iloc slicing from there needs no
    # reset_index since the loop below iterates via itertuples(index=False),
    # which never looks at the DataFrame's index labels.
    window_start = m1["timestamp"].searchsorted(entry_timestamp, side="right")
    window = m1.iloc[window_start:]
    closed = False
    # Task 7.4 PERFORMANCE NOTE: window.iloc[i] (pandas positional row
    # access, constructing a full Series per candle) was called once per
    # candle in the trade's lifetime; profiling showed this among the
    # remaining hotspots. itertuples(index=False) is a lazy iterator over
    # cheap namedtuples -- critically, unlike converting the whole window
    # to a list/dict upfront, it does NOT materialize candles the loop
    # never reaches (most trades close within a handful of candles, but
    # `window` itself spans from entry_timestamp to the END of the whole
    # dataset). Field access changes from candle["field"] to candle.field
    # accordingly, including inside the check_breakeven/check_trailing_stop/
    # check_session_close helpers in src/backtest/management.py, which
    # receive this same candle object.
    last_candle = None

    for candle in window.itertuples(index=False):
        last_candle = candle
        trade.duration_candles += 1

        stop_hit = (candle.low <= trade.current_stop_loss) if signal.direction == "bullish" else (candle.high >= trade.current_stop_loss)
        if stop_hit:
            exit_price = fill_exit(trade.current_stop_loss, signal.direction, is_stop_exit=True, config=execution_config)
            pnl = compute_pnl(signal.direction, entry_price, exit_price, remaining_lots, execution_config)
            commission = compute_commission(remaining_lots, execution_config)
            total_pnl += pnl
            total_commission += commission
            trade.exit_price, trade.exit_timestamp = exit_price, candle.timestamp
            trade.exit_reason = last_stop_move_reason
            closed = True

        if not closed:
            for level_idx, (level_price, fraction) in enumerate(list(trade.take_profit_levels)):
                if fraction <= 0:
                    continue
                tp_hit = (candle.high >= level_price) if signal.direction == "bullish" else (candle.low <= level_price)
                if not tp_hit:
                    continue
                exit_lots = lots * fraction
                exit_price = fill_exit(level_price, signal.direction, is_stop_exit=False, config=execution_config)
                pnl = compute_pnl(signal.direction, entry_price, exit_price, exit_lots, execution_config)
                commission = compute_commission(exit_lots, execution_config)
                total_pnl += pnl
                total_commission += commission
                trade.remaining_fraction -= fraction
                trade.take_profit_levels[level_idx] = (level_price, 0.0)
                is_final = trade.remaining_fraction <= 1e-9
                if is_final:
                    trade.exit_price, trade.exit_timestamp = exit_price, candle.timestamp
                    trade.exit_reason = ExitReason.TAKE_PROFIT.value
                    closed = True
                else:
                    trade.partial_exits.append({
                        "timestamp": candle.timestamp, "price": exit_price,
                        "fraction": fraction, "pnl": pnl,
                    })
                    trade.add_management_event(candle.timestamp, "PARTIAL_EXIT", price=exit_price, fraction=fraction)
                    remaining_lots = lots * trade.remaining_fraction
                break  # only one TP level can trigger per candle, nearest first

        if closed:
            break

        favorable = (candle.high - trade.entry_price) if signal.direction == "bullish" else (trade.entry_price - candle.low)
        adverse = (trade.entry_price - candle.low) if signal.direction == "bullish" else (candle.high - trade.entry_price)
        trade.mfe = max(trade.mfe, favorable)
        trade.mae = max(trade.mae, adverse)

        # Task 7.4 PERFORMANCE NOTE: history_so_far is only ever read inside
        # check_trailing_stop's "atr" branch (see src/backtest/management.py) --
        # check_breakeven never uses it, and the "fixed_pips"/"structure"/None
        # trailing methods never touch their m1_so_far parameter either.
        # Profiling showed this full-dataset boolean filter (recomputed on
        # EVERY candle of EVERY open trade, over the entire m1 frame -- not
        # just the trade's own window) as the single largest remaining
        # bottleneck under the default config, where trailing_method is None
        # and the result was always discarded unused. Skipping it whenever
        # the atr trailing method isn't active is behavior-identical --
        # check_trailing_stop's other branches never read this argument.
        history_so_far = (
            m1[m1["timestamp"] <= candle.timestamp]
            if management_config.trailing_method == "atr"
            else None
        )

        # check_breakeven/check_trailing_stop are public functions tested
        # (and used elsewhere) with dict-style candle["field"] access, so
        # convert only THIS single visited candle -- cheap and bounded by
        # trade duration, unlike materializing the whole window upfront.
        candle_map = candle._asdict()

        be_stop = check_breakeven(trade, candle_map, management_config, execution_config.pip_size)
        if be_stop is not None:
            trade.current_stop_loss = be_stop
            last_stop_move_reason = ExitReason.BREAKEVEN_STOP.value
            trade.add_management_event(candle.timestamp, "BREAKEVEN_MOVED", new_stop=be_stop)

        trail_stop = check_trailing_stop(trade, candle_map, history_so_far, management_config, execution_config.pip_size)
        if trail_stop is not None:
            trade.current_stop_loss = trail_stop
            last_stop_move_reason = ExitReason.TRAILING_STOP.value
            trade.add_management_event(candle.timestamp, "TRAILING_STOP_MOVED", new_stop=trail_stop)

        if check_max_duration(trade, management_config):
            exit_price = candle.close
            pnl = compute_pnl(signal.direction, entry_price, exit_price, remaining_lots, execution_config)
            total_pnl += pnl
            total_commission += compute_commission(remaining_lots, execution_config)
            trade.exit_price, trade.exit_timestamp = exit_price, candle.timestamp
            trade.exit_reason = ExitReason.MAX_DURATION.value
            closed = True
            break

        if check_session_close(trade, candle.timestamp, context, management_config):
            exit_price = candle.close
            pnl = compute_pnl(signal.direction, entry_price, exit_price, remaining_lots, execution_config)
            total_pnl += pnl
            total_commission += compute_commission(remaining_lots, execution_config)
            trade.exit_price, trade.exit_timestamp = exit_price, candle.timestamp
            trade.exit_reason = ExitReason.SESSION_CLOSE.value
            closed = True
            break

    if not closed:
        if last_candle is not None:
            exit_price = last_candle.close
            pnl = compute_pnl(signal.direction, entry_price, exit_price, remaining_lots, execution_config)
            total_pnl += pnl
            total_commission += compute_commission(remaining_lots, execution_config)
            trade.exit_price, trade.exit_timestamp = exit_price, last_candle.timestamp
        else:
            trade.exit_price, trade.exit_timestamp = trade.entry_price, trade.entry_timestamp
        trade.exit_reason = ExitReason.END_OF_DATA.value

    trade.status = TradeStatus.CLOSED.value
    trade.commission_paid = total_commission
    trade.realized_pnl = total_pnl - total_commission
    trade.r_multiple = round(trade.realized_pnl / risk_amount, 4) if risk_amount > 0 else None

    risk_tracker.register_close(trade.exit_timestamp, risk_amount, trade.realized_pnl)
    return trade


def run_backtest(
    signals: list, m1: pd.DataFrame, context=None,
    entry_config: EntryConfig = DEFAULT_ENTRY_CONFIG,
    stop_config: StopLossConfig = DEFAULT_STOP_LOSS_CONFIG,
    tp_config: TakeProfitConfig = DEFAULT_TAKE_PROFIT_CONFIG,
    execution_config: ExecutionConfig = DEFAULT_EXECUTION_CONFIG,
    risk_config: RiskConfig = DEFAULT_RISK_CONFIG,
    management_config: ManagementConfig = DEFAULT_MANAGEMENT_CONFIG,
    progress_cb=None,
) -> list:
    """Runs every signal (sorted chronologically) through the full trade
    lifecycle against a SHARED RiskTracker, so portfolio-level limits
    (max simultaneous positions, daily/weekly loss, consecutive losses,
    exposure) are enforced across the whole signal stream, not per-signal
    in isolation.

    progress_cb: optional Task 7.4 Objective 5 hook, called as
    `progress_cb(trades_processed, total_signals)` after each signal is
    resolved into a trade. Purely an observability side-channel -- it
    receives only counters, never trade data, so it cannot influence
    trading logic. Defaults to None (no-op)."""
    risk_tracker = RiskTracker(config=risk_config)
    entries_today: dict = {}
    trades = []
    sorted_signals = sorted(signals, key=lambda s: s.timestamp)
    total_signals = len(sorted_signals)

    for i, signal in enumerate(sorted_signals):
        day_key = signal.timestamp.date()
        if daily_trade_limit_reached(entries_today.get(day_key, 0), management_config):
            trade = Trade(
                trade_id=f"TRADE_{signal.signal_id}", signal_id=signal.signal_id, strategy_id=signal.strategy_id,
                symbol=signal.symbol, timeframe=signal.timeframe, direction=signal.direction,
                signal_timestamp=signal.timestamp, confidence_score=signal.confidence_score,
                reason_codes=signal.reason_codes, confluence_snapshot=signal.confluence_snapshot,
                entry_method=entry_config.method, stop_method=stop_config.method, take_profit_method=tp_config.method,
                session=signal.session, status=TradeStatus.REJECTED.value, rejection_reason="DailyTradeLimitReached",
            )
            trades.append(trade)
            if progress_cb is not None:
                progress_cb(i + 1, total_signals)
            continue

        trade = simulate_trade(
            signal, m1, context, risk_tracker,
            entry_config, stop_config, tp_config, execution_config, risk_config, management_config,
        )
        if trade.status == TradeStatus.OPEN.value or trade.status == TradeStatus.CLOSED.value:
            entries_today[day_key] = entries_today.get(day_key, 0) + 1
        trades.append(trade)
        if progress_cb is not None:
            progress_cb(i + 1, total_signals)

    return trades
