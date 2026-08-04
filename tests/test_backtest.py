"""
Task 4 Backtest & Execution Simulator tests.

Most tests construct a minimal `Signal` directly (bypassing the Task 3
strategy engines entirely) against small, hand-crafted M1 DataFrames --
this keeps each backtest component's test precise and fast, mirroring
the style already used for Task 1/2. A handful of integration-level tests
at the end run the full Task 3 -> Task 4 pipeline for properties that can
only be checked end-to-end (no-look-ahead, reproducibility, portfolio
comparison).
"""

import pandas as pd
import pytest

from config.settings import (
    EntryConfig, StopLossConfig, TakeProfitConfig, ExecutionConfig, RiskConfig, ManagementConfig,
)
from src.strategies.common import Signal
from src.backtest.trade import Trade, TradeStatus, ExitReason
from src.backtest.entry import resolve_entry
from src.backtest.stop_loss import resolve_stop_loss
from src.backtest.take_profit import resolve_take_profit
from src.backtest.execution import apply_spread_at_entry, apply_slippage, fill_entry, fill_exit, compute_commission, compute_pnl
from src.backtest.risk import resolve_position_size, RiskTracker
from src.backtest.management import check_breakeven, check_trailing_stop, check_max_duration, daily_trade_limit_reached
from src.backtest.engine import simulate_trade, run_backtest
from src.backtest.metrics import compute_performance_metrics, build_equity_curve, max_drawdown, trades_to_dataframe
from src.backtest.portfolio import combine_trades, compare_strategies, strategy_correlation
from src.backtest.walkforward import split_dataset, split_dataframes
from tests.helpers import make_candles

PIP = 0.0001


def _signal(direction="bullish", timestamp="2024-01-01 00:10:00", entry_zone=(1.0990, 1.1000), metadata=None, confluence_snapshot=None):
    ts = pd.Timestamp(timestamp)
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    return Signal(
        signal_id="SIG1", strategy_id="S3", timestamp=ts,
        symbol="TEST", timeframe="M1", direction=direction, entry_zone=entry_zone,
        stop_loss_reference=entry_zone[0], target_reference=entry_zone[1] + 0.002,
        confidence_score=90.0, reason_codes=["S3", "Confidence90"],
        confluence_snapshot=confluence_snapshot or {}, market_structure_state="BULLISH",
        session="london", risk_reference={}, metadata=metadata or {},
    )


def _flat_m1(n=30, price=1.10, start="2024-01-01 00:00:00"):
    rows = [(price, price + 0.0002, price - 0.0002, price) for _ in range(n)]
    return make_candles(rows, start=start)


# ---------------------------------------------------------------------------
# Entry Engine
# ---------------------------------------------------------------------------


def test_entry_market_uses_next_candle_open():
    m1 = _flat_m1()
    signal = _signal(timestamp="2024-01-01 00:05:00")
    result = resolve_entry(signal, m1, EntryConfig(method="market"), PIP)
    next_candle = m1[m1["timestamp"] > signal.timestamp].iloc[0]
    assert result["entry_price"] == pytest.approx(next_candle["open"])
    assert result["entry_timestamp"] == next_candle["timestamp"]


def test_entry_confirmation_close_uses_signal_candle_close():
    m1 = _flat_m1()
    signal = _signal(timestamp="2024-01-01 00:05:00")
    result = resolve_entry(signal, m1, EntryConfig(method="confirmation_close"), PIP)
    row = m1[m1["timestamp"] == signal.timestamp].iloc[0]
    assert result["entry_price"] == pytest.approx(row["close"])
    assert result["entry_timestamp"] == signal.timestamp


def test_entry_ob_touch_and_midpoint_and_edges():
    # bullish OB zone [1.0990, 1.1000]; price dips down to touch it a few candles later
    rows = [(1.1010, 1.1012, 1.1008, 1.1010)] * 5 + [(1.1010, 1.1010, 1.0985, 1.0990)] + [(1.0990, 1.0992, 1.0988, 1.0990)] * 5
    m1 = make_candles(rows, start="2024-01-01 00:00:00")
    signal = _signal(direction="bullish", timestamp=m1["timestamp"].iloc[0], entry_zone=(1.0990, 1.1000))

    touch = resolve_entry(signal, m1, EntryConfig(method="ob_touch"), PIP)
    assert touch["entry_price"] == pytest.approx(1.1000)  # proximal edge (top) for a bullish OB

    proximal = resolve_entry(signal, m1, EntryConfig(method="ob_proximal_edge"), PIP)
    assert proximal["entry_price"] == pytest.approx(1.1000)

    distal = resolve_entry(signal, m1, EntryConfig(method="ob_distal_edge"), PIP)
    assert distal["entry_price"] == pytest.approx(1.0990)  # the candle dipped to 1.0985, past the distal edge

    midpoint = resolve_entry(signal, m1, EntryConfig(method="ob_midpoint"), PIP)
    assert midpoint["entry_price"] == pytest.approx(1.0995)


def test_entry_expires_when_zone_never_touched():
    m1 = _flat_m1(n=5, price=1.20)  # nowhere near the OB zone
    signal = _signal(direction="bullish", timestamp=m1["timestamp"].iloc[0], entry_zone=(1.0990, 1.1000))
    result = resolve_entry(signal, m1, EntryConfig(method="ob_touch", max_wait_candles=3), PIP)
    assert result is None


# ---------------------------------------------------------------------------
# Stop-Loss Engine
# ---------------------------------------------------------------------------


def test_stop_loss_fixed_pips():
    signal = _signal(direction="bullish")
    result = resolve_stop_loss(signal, 1.1000, signal.timestamp, _flat_m1(), None, StopLossConfig(method="fixed_pips", fixed_pips=20), PIP)
    assert result["stop_loss"] == pytest.approx(1.1000 - 20 * PIP)
    assert result["method"] == "fixed_pips"


def test_stop_loss_ob_extreme_bullish_and_bearish():
    bullish = _signal(direction="bullish", entry_zone=(1.0990, 1.1000))
    r = resolve_stop_loss(bullish, 1.1000, bullish.timestamp, _flat_m1(), None, StopLossConfig(method="ob_extreme", buffer_pips=2), PIP)
    assert r["stop_loss"] == pytest.approx(1.0990 - 2 * PIP)

    bearish = _signal(direction="bearish", entry_zone=(1.1000, 1.1010))
    r2 = resolve_stop_loss(bearish, 1.1000, bearish.timestamp, _flat_m1(), None, StopLossConfig(method="ob_extreme", buffer_pips=2), PIP)
    assert r2["stop_loss"] == pytest.approx(1.1010 + 2 * PIP)


def test_stop_loss_percentage():
    signal = _signal(direction="bullish")
    r = resolve_stop_loss(signal, 1.1000, signal.timestamp, _flat_m1(), None, StopLossConfig(method="percentage", percentage=0.01), PIP)
    assert r["stop_loss"] == pytest.approx(1.1000 * 0.99)


def test_stop_loss_atr_multiple_falls_back_when_no_history():
    signal = _signal(direction="bullish", timestamp="2024-01-01 00:00:00")
    m1 = _flat_m1()
    r = resolve_stop_loss(signal, 1.1000, m1["timestamp"].iloc[0], m1, None, StopLossConfig(method="atr_multiple"), PIP)
    assert r["stop_loss"] < 1.1000


# ---------------------------------------------------------------------------
# Take-Profit Engine (including gap targets and partial exits)
# ---------------------------------------------------------------------------


def test_take_profit_fixed_rr():
    signal = _signal(direction="bullish")
    levels = resolve_take_profit(signal, 1.1000, 1.0980, signal.timestamp, _flat_m1(), None, TakeProfitConfig(method="fixed_rr", risk_reward=2.0), PIP)
    assert len(levels) == 1
    assert levels[0][0] == pytest.approx(1.1000 + 2 * 0.0020)
    assert levels[0][1] == pytest.approx(1.0)


def test_take_profit_gap_fill_targets():
    signal = _signal(direction="bearish", metadata={"friday_close": 1.1000, "reopen_open": 1.1080})
    for method, fraction in (("gap_fill_25", 0.25), ("gap_fill_50", 0.50), ("gap_fill_100", 1.0)):
        levels = resolve_take_profit(signal, 1.1050, 1.1090, signal.timestamp, _flat_m1(), None, TakeProfitConfig(method=method), PIP)
        expected = 1.1080 + fraction * (1.1000 - 1.1080)
        assert levels[-1][0] == pytest.approx(expected)


def test_take_profit_partial_exits_ordered_nearest_first():
    signal = _signal(direction="bullish")
    config = TakeProfitConfig(method="fixed_rr", risk_reward=3.0, partial_exits=((1.0, 0.5), (2.0, 0.3)))
    levels = resolve_take_profit(signal, 1.1000, 1.0980, signal.timestamp, _flat_m1(), None, config, PIP)
    prices = [lv[0] for lv in levels]
    assert prices == sorted(prices)
    fractions = [lv[1] for lv in levels]
    assert sum(fractions) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Execution model: spread, slippage, commission
# ---------------------------------------------------------------------------


def test_spread_applied_at_entry_both_directions():
    config = ExecutionConfig(spread_pips=1.0, pip_size=PIP)
    assert apply_spread_at_entry(1.1000, "bullish", config) == pytest.approx(1.1000 + 1 * PIP)
    assert apply_spread_at_entry(1.1000, "bearish", config) == pytest.approx(1.1000 - 1 * PIP)


def test_slippage_only_applied_on_adverse_exits():
    config = ExecutionConfig(slippage_pips=0.5, pip_size=PIP)
    assert apply_slippage(1.1000, "bullish", is_adverse_exit=False, config=config) == pytest.approx(1.1000)
    assert apply_slippage(1.1000, "bullish", is_adverse_exit=True, config=config) == pytest.approx(1.1000 + 0.5 * PIP)


def test_fill_exit_slips_only_on_stop_exit():
    config = ExecutionConfig(slippage_pips=0.5, pip_size=PIP)
    tp_fill = fill_exit(1.1000, "bullish", is_stop_exit=False, config=config)
    stop_fill = fill_exit(1.1000, "bullish", is_stop_exit=True, config=config)
    assert tp_fill == pytest.approx(1.1000)
    assert stop_fill < tp_fill


def test_commission_scales_with_lots():
    config = ExecutionConfig(commission_per_lot=7.0)
    assert compute_commission(0.5, config) == pytest.approx(3.5)
    assert compute_commission(2.0, config) == pytest.approx(14.0)


def test_compute_pnl_bullish_and_bearish():
    config = ExecutionConfig(contract_size=100_000)
    bullish_pnl = compute_pnl("bullish", 1.1000, 1.1050, 1.0, config)
    assert bullish_pnl == pytest.approx(0.0050 * 100_000)
    bearish_pnl = compute_pnl("bearish", 1.1000, 1.0950, 1.0, config)
    assert bearish_pnl == pytest.approx(0.0050 * 100_000)


# ---------------------------------------------------------------------------
# Risk sizing + RiskTracker gates
# ---------------------------------------------------------------------------


def test_fixed_percentage_risk_sizing():
    risk_config = RiskConfig(sizing_method="fixed_percentage_risk", risk_per_trade_pct=0.01, starting_balance=10_000)
    exec_config = ExecutionConfig(contract_size=100_000)
    lots = resolve_position_size(10_000, stop_distance=0.0020, config=risk_config, execution_config=exec_config)
    expected = (10_000 * 0.01) / (0.0020 * 100_000)
    assert lots == pytest.approx(round(expected, 2), abs=0.01)


def test_risk_tracker_blocks_max_simultaneous_positions():
    tracker = RiskTracker(config=RiskConfig(max_simultaneous_positions=1))
    ts = pd.Timestamp("2024-01-01", tz="UTC")
    allowed, _ = tracker.can_open(ts, risk_amount=10.0)
    assert allowed
    tracker.register_open(10.0)
    allowed2, reason = tracker.can_open(ts, risk_amount=10.0)
    assert not allowed2 and reason == "MaxSimultaneousPositions"


def test_risk_tracker_blocks_after_max_daily_loss():
    tracker = RiskTracker(config=RiskConfig(max_daily_loss_pct=0.01, starting_balance=10_000))
    ts = pd.Timestamp("2024-01-01 10:00", tz="UTC")
    tracker.register_open(50.0)
    tracker.register_close(ts, risk_amount=50.0, realized_pnl=-150.0)  # 1.5% loss > 1% limit
    allowed, reason = tracker.can_open(ts, risk_amount=10.0)
    assert not allowed and reason == "MaxDailyLossReached"


def test_risk_tracker_blocks_after_max_consecutive_losses():
    tracker = RiskTracker(config=RiskConfig(max_consecutive_losses=2, max_daily_loss_pct=1.0))
    for i in range(2):
        ts = pd.Timestamp("2024-01-01", tz="UTC") + pd.Timedelta(hours=i)
        tracker.register_open(10.0)
        tracker.register_close(ts, risk_amount=10.0, realized_pnl=-5.0)
    allowed, reason = tracker.can_open(pd.Timestamp("2024-01-01 05:00", tz="UTC"), risk_amount=10.0)
    assert not allowed and reason == "ConsecutiveLossCooldownActive"


def test_risk_tracker_consecutive_loss_cooldown_expires_and_resets():
    """Task 11 Phase 1 fix: the consecutive-loss gate must be a temporary
    circuit breaker, not a permanent lockout -- it has to actually let new
    trades open again once the configured cooldown elapses, with the
    streak counter reset (not just re-armed at the same trip point)."""
    config = RiskConfig(max_consecutive_losses=2, max_daily_loss_pct=1.0, max_weekly_loss_pct=1.0, consecutive_loss_cooldown_days=1.0)
    tracker = RiskTracker(config=config)
    for i in range(2):
        ts = pd.Timestamp("2024-01-01", tz="UTC") + pd.Timedelta(hours=i)
        tracker.register_open(10.0)
        tracker.register_close(ts, risk_amount=10.0, realized_pnl=-5.0)

    still_cooling_down = pd.Timestamp("2024-01-01 12:00", tz="UTC")
    allowed, reason = tracker.can_open(still_cooling_down, risk_amount=10.0)
    assert not allowed and reason == "ConsecutiveLossCooldownActive"

    after_cooldown = pd.Timestamp("2024-01-02 01:00", tz="UTC")  # > 1 day after the 2nd loss
    allowed, reason = tracker.can_open(after_cooldown, risk_amount=10.0)
    assert allowed and reason is None
    assert tracker.consecutive_losses == 0
    assert tracker.locked_out_until is None


def test_risk_tracker_winning_trade_clears_cooldown_immediately():
    """A winning close should reset the streak and clear any pending
    lockout right away, without waiting for the cooldown to elapse."""
    config = RiskConfig(max_consecutive_losses=2, max_daily_loss_pct=1.0, max_weekly_loss_pct=1.0, consecutive_loss_cooldown_days=1.0)
    tracker = RiskTracker(config=config)
    ts0 = pd.Timestamp("2024-01-01", tz="UTC")
    for i in range(2):
        ts = ts0 + pd.Timedelta(hours=i)
        tracker.register_open(10.0)
        tracker.register_close(ts, risk_amount=10.0, realized_pnl=-5.0)
    assert tracker.locked_out_until is not None

    tracker.register_open(10.0)
    tracker.register_close(ts0 + pd.Timedelta(hours=2), risk_amount=10.0, realized_pnl=20.0)
    assert tracker.consecutive_losses == 0
    assert tracker.locked_out_until is None
    allowed, reason = tracker.can_open(ts0 + pd.Timedelta(hours=3), risk_amount=10.0)
    assert allowed and reason is None


def test_no_martingale_sizing_does_not_depend_on_prior_losses():
    """Fixed-percentage-risk sizing must be identical for the same stop
    distance regardless of the account's trade history -- there is no
    code path where a prior loss increases the next trade's size."""
    risk_config = RiskConfig(sizing_method="fixed_percentage_risk", risk_per_trade_pct=0.01)
    exec_config = ExecutionConfig()
    lots_after_win = resolve_position_size(10_500, 0.0020, risk_config, exec_config)
    lots_after_loss = resolve_position_size(9_500, 0.0020, risk_config, exec_config)
    lots_fresh = resolve_position_size(10_000, 0.0020, risk_config, exec_config)
    # sizes scale only with CURRENT balance and CURRENT stop distance, monotonically
    assert lots_after_loss < lots_fresh < lots_after_win


# ---------------------------------------------------------------------------
# Trade management: breakeven, trailing stop, max duration, daily limit
# ---------------------------------------------------------------------------


def test_breakeven_moves_stop_once_r_target_reached():
    trade = Trade(
        trade_id="T1", signal_id="S1", strategy_id="S3", symbol="TEST", timeframe="M1", direction="bullish",
        signal_timestamp=pd.Timestamp("2024-01-01", tz="UTC"), confidence_score=90, reason_codes=[], confluence_snapshot={},
        entry_method="market", stop_method="fixed_pips", take_profit_method="fixed_rr",
        entry_price=1.1000, initial_stop_loss=1.0980, current_stop_loss=1.0980,
    )
    candle_not_yet = {"high": 1.1010, "low": 1.0995}
    assert check_breakeven(trade, candle_not_yet, ManagementConfig(breakeven_trigger_r=1.0), PIP) is None

    candle_reached = {"high": 1.1025, "low": 1.1000}  # favorable = 0.0025 >= 1R (0.0020)
    new_stop = check_breakeven(trade, candle_reached, ManagementConfig(breakeven_trigger_r=1.0, breakeven_buffer_pips=0), PIP)
    assert new_stop == pytest.approx(1.1000)


def test_trailing_stop_fixed_pips_only_moves_favorably():
    trade = Trade(
        trade_id="T1", signal_id="S1", strategy_id="S3", symbol="TEST", timeframe="M1", direction="bullish",
        signal_timestamp=pd.Timestamp("2024-01-01", tz="UTC"), confidence_score=90, reason_codes=[], confluence_snapshot={},
        entry_method="market", stop_method="fixed_pips", take_profit_method="fixed_rr",
        entry_price=1.1000, initial_stop_loss=1.0980, current_stop_loss=1.0980,
    )
    config = ManagementConfig(trailing_method="fixed_pips", trailing_fixed_pips=10)
    candle = {"high": 1.1050, "low": 1.1040}
    new_stop = check_trailing_stop(trade, candle, _flat_m1(), config, PIP)
    assert new_stop == pytest.approx(1.1050 - 10 * PIP)

    trade.current_stop_loss = new_stop
    worse_candle = {"high": 1.1030, "low": 1.1020}  # lower high -> must NOT move the stop backward
    should_be_none = check_trailing_stop(trade, worse_candle, _flat_m1(), config, PIP)
    assert should_be_none is None


def test_max_duration_exit():
    trade = Trade(
        trade_id="T1", signal_id="S1", strategy_id="S3", symbol="TEST", timeframe="M1", direction="bullish",
        signal_timestamp=pd.Timestamp("2024-01-01", tz="UTC"), confidence_score=90, reason_codes=[], confluence_snapshot={},
        entry_method="market", stop_method="fixed_pips", take_profit_method="fixed_rr", duration_candles=100,
    )
    assert check_max_duration(trade, ManagementConfig(max_trade_duration_candles=100))
    assert not check_max_duration(trade, ManagementConfig(max_trade_duration_candles=200))


def test_daily_trade_limit():
    assert daily_trade_limit_reached(3, ManagementConfig(daily_trade_limit=3))
    assert not daily_trade_limit_reached(2, ManagementConfig(daily_trade_limit=3))
    assert not daily_trade_limit_reached(100, ManagementConfig(daily_trade_limit=None))


# ---------------------------------------------------------------------------
# Full trade simulation: stop-loss exit, take-profit exit, partial exits
# ---------------------------------------------------------------------------


def test_simulate_trade_stop_loss_exit():
    rows = [(1.1000, 1.1005, 1.0995, 1.1000)] * 3 + [(1.1000, 1.1002, 1.0975, 1.0980)]  # drops through stop
    m1 = make_candles(rows, start="2024-01-01 00:00:00")
    signal = _signal(direction="bullish", timestamp=m1["timestamp"].iloc[0], entry_zone=(1.0990, 1.1000))
    tracker = RiskTracker(config=RiskConfig())
    trade = simulate_trade(
        signal, m1, None, tracker,
        EntryConfig(method="market"), StopLossConfig(method="fixed_pips", fixed_pips=15),
        TakeProfitConfig(method="fixed_rr", risk_reward=5.0), ExecutionConfig(), RiskConfig(), ManagementConfig(breakeven_trigger_r=None),
    )
    assert trade.status == TradeStatus.CLOSED.value
    assert trade.exit_reason == ExitReason.STOP_LOSS.value
    assert trade.realized_pnl < 0
    assert trade.r_multiple < 0


def test_simulate_trade_take_profit_exit():
    rows = [(1.1000, 1.1005, 1.0995, 1.1000)] * 3 + [(1.1000, 1.1050, 1.0998, 1.1040)]
    m1 = make_candles(rows, start="2024-01-01 00:00:00")
    signal = _signal(direction="bullish", timestamp=m1["timestamp"].iloc[0], entry_zone=(1.0990, 1.1000))
    tracker = RiskTracker(config=RiskConfig())
    trade = simulate_trade(
        signal, m1, None, tracker,
        EntryConfig(method="market"), StopLossConfig(method="fixed_pips", fixed_pips=15),
        TakeProfitConfig(method="fixed_rr", risk_reward=1.5), ExecutionConfig(), RiskConfig(), ManagementConfig(breakeven_trigger_r=None),
    )
    assert trade.status == TradeStatus.CLOSED.value
    assert trade.exit_reason == ExitReason.TAKE_PROFIT.value
    assert trade.realized_pnl > 0
    assert trade.r_multiple > 0


def test_simulate_trade_partial_exit_then_final_target():
    rows = [(1.1000, 1.1005, 1.0995, 1.1000)] * 2 \
        + [(1.1000, 1.1035, 1.0998, 1.1030)] \
        + [(1.1030, 1.1035, 1.1020, 1.1030)] \
        + [(1.1030, 1.1075, 1.1020, 1.1070)]
    m1 = make_candles(rows, start="2024-01-01 00:00:00")
    signal = _signal(direction="bullish", timestamp=m1["timestamp"].iloc[0], entry_zone=(1.0990, 1.1000))
    tracker = RiskTracker(config=RiskConfig())
    tp_config = TakeProfitConfig(method="fixed_rr", risk_reward=3.0, partial_exits=((1.5, 0.5),))
    trade = simulate_trade(
        signal, m1, None, tracker,
        EntryConfig(method="market"), StopLossConfig(method="fixed_pips", fixed_pips=15),
        tp_config, ExecutionConfig(), RiskConfig(), ManagementConfig(breakeven_trigger_r=None),
    )
    assert len(trade.partial_exits) == 1
    assert trade.exit_reason == ExitReason.TAKE_PROFIT.value
    assert trade.status == TradeStatus.CLOSED.value


def test_simulate_trade_expires_without_error():
    m1 = _flat_m1(n=3, price=1.20)
    signal = _signal(direction="bullish", timestamp=m1["timestamp"].iloc[0], entry_zone=(1.0990, 1.1000))
    tracker = RiskTracker(config=RiskConfig())
    trade = simulate_trade(
        signal, m1, None, tracker,
        EntryConfig(method="ob_touch", max_wait_candles=2), StopLossConfig(), TakeProfitConfig(), ExecutionConfig(), RiskConfig(), ManagementConfig(),
    )
    assert trade.status == TradeStatus.EXPIRED.value


# ---------------------------------------------------------------------------
# Performance metrics / equity curve / drawdown
# ---------------------------------------------------------------------------


def _closed_trade(pnl: float, exit_ts: str, strategy_id="S3", symbol="TEST", session="london", confidence=90.0, r=None) -> Trade:
    return Trade(
        trade_id=f"T_{exit_ts}", signal_id="S", strategy_id=strategy_id, symbol=symbol, timeframe="M1", direction="bullish",
        signal_timestamp=pd.Timestamp(exit_ts, tz="UTC"), confidence_score=confidence, reason_codes=[], confluence_snapshot={},
        entry_method="market", stop_method="fixed_pips", take_profit_method="fixed_rr",
        status=TradeStatus.CLOSED.value, entry_price=1.10, exit_price=1.10 + pnl / 100_000,
        exit_timestamp=pd.Timestamp(exit_ts, tz="UTC"), exit_reason="TAKE_PROFIT" if pnl > 0 else "STOP_LOSS",
        realized_pnl=pnl, r_multiple=r if r is not None else pnl / 100.0, duration_candles=10, mae=abs(min(pnl, 0)), mfe=max(pnl, 0),
        session=session,
    )


def test_performance_metrics_basic_correctness():
    trades = [
        _closed_trade(100, "2024-01-01"), _closed_trade(-50, "2024-01-02"),
        _closed_trade(150, "2024-01-03"), _closed_trade(-30, "2024-01-04"),
    ]
    metrics = compute_performance_metrics(trades, starting_balance=10_000)
    assert metrics["net_profit"] == pytest.approx(170.0)
    assert metrics["win_rate"] == pytest.approx(0.5)
    assert metrics["gross_profit"] == pytest.approx(250.0)
    assert metrics["gross_loss"] == pytest.approx(-80.0)
    assert metrics["profit_factor"] == pytest.approx(250.0 / 80.0)
    assert metrics["expectancy"] == pytest.approx(170.0 / 4)


def test_max_consecutive_wins_and_losses():
    trades = [_closed_trade(10, "2024-01-01"), _closed_trade(10, "2024-01-02"), _closed_trade(-5, "2024-01-03"),
              _closed_trade(-5, "2024-01-04"), _closed_trade(-5, "2024-01-05"), _closed_trade(10, "2024-01-06")]
    metrics = compute_performance_metrics(trades, starting_balance=10_000)
    assert metrics["max_consecutive_wins"] == 2
    assert metrics["max_consecutive_losses"] == 3


def test_equity_curve_and_max_drawdown():
    trades = [_closed_trade(100, "2024-01-01"), _closed_trade(-200, "2024-01-02"), _closed_trade(50, "2024-01-03")]
    curve = build_equity_curve(trades, starting_balance=1000)
    assert list(curve["balance"]) == [1000, 1100, 900, 950]
    dd = max_drawdown(curve)
    assert dd["max_drawdown"] == pytest.approx(-200.0)


def test_expectancy_breakdowns_present():
    trades = [
        _closed_trade(100, "2024-01-01", strategy_id="S3", session="london"),
        _closed_trade(-50, "2024-01-02", strategy_id="S5", session="new_york"),
    ]
    metrics = compute_performance_metrics(trades, starting_balance=10_000)
    assert "S3" in metrics["expectancy_by_strategy"]
    assert "S5" in metrics["expectancy_by_strategy"]
    assert "london" in metrics["expectancy_by_session"]
    assert "bullish" in metrics["expectancy_by_direction"]


def test_trade_history_dataframe_generation():
    trades = [_closed_trade(100, "2024-01-01"), _closed_trade(-50, "2024-01-02")]
    df = trades_to_dataframe(trades)
    assert len(df) == 2
    assert "realized_pnl" in df.columns


# ---------------------------------------------------------------------------
# Portfolio: combination, comparison, correlation
# ---------------------------------------------------------------------------


def test_portfolio_combine_and_compare():
    by_strategy = {
        "S3": [_closed_trade(100, "2024-01-01", strategy_id="S3"), _closed_trade(-40, "2024-01-02", strategy_id="S3")],
        "S5": [_closed_trade(200, "2024-01-01", strategy_id="S5"), _closed_trade(200, "2024-01-02", strategy_id="S5")],
    }
    combined = combine_trades(by_strategy)
    assert len(combined) == 4

    comparison = compare_strategies(by_strategy, starting_balance=10_000)
    assert set(comparison["strategy_id"]) == {"S3", "S5"}
    s5_row = comparison[comparison.strategy_id == "S5"].iloc[0]
    assert s5_row["win_rate"] == pytest.approx(1.0)


def test_strategy_correlation_matrix_shape():
    by_strategy = {
        "S3": [_closed_trade(100, "2024-01-01", strategy_id="S3"), _closed_trade(-40, "2024-01-03", strategy_id="S3")],
        "S5": [_closed_trade(-80, "2024-01-01", strategy_id="S5"), _closed_trade(60, "2024-01-03", strategy_id="S5")],
    }
    corr = strategy_correlation(by_strategy)
    assert list(corr.columns) == ["S3", "S5"]
    assert corr.loc["S3", "S3"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Walk-forward splitting
# ---------------------------------------------------------------------------


def test_walk_forward_split_proportions_and_period_classification():
    m1 = _flat_m1(n=100)
    split = split_dataset(m1, train_pct=0.6, validation_pct=0.2)
    dfs = split_dataframes(m1, split)
    assert abs(len(dfs["train"]) - 60) <= 1
    assert abs(len(dfs["validation"]) - 20) <= 1
    assert abs(len(dfs["out_of_sample"]) - 20) <= 1
    assert split.period_of(dfs["train"]["timestamp"].iloc[0]) == "train"
    assert split.period_of(dfs["out_of_sample"]["timestamp"].iloc[-1]) == "out_of_sample"


# ---------------------------------------------------------------------------
# No look-ahead bias + deterministic repeatability (integration-level)
# ---------------------------------------------------------------------------


def test_backtest_engine_no_lookahead():
    """A trade that has ALREADY CLOSED must not change if we extend the
    M1 data far beyond its exit with a completely different (adverse)
    future price move."""
    rows = [(1.1000, 1.1005, 1.0995, 1.1000)] * 3 + [(1.1000, 1.1050, 1.0998, 1.1040)]
    m1_short = make_candles(rows, start="2024-01-01 00:00:00")
    signal = _signal(direction="bullish", timestamp=m1_short["timestamp"].iloc[0], entry_zone=(1.0990, 1.1000))

    extra_rows = [(1.1040, 1.1045, 1.0800, 1.0810)] * 5  # a big future crash, irrelevant to an already-closed trade
    m1_long = pd.concat([m1_short, make_candles(extra_rows, start=str(m1_short["timestamp"].iloc[-1] + pd.Timedelta(minutes=1)))], ignore_index=True)

    tp_config = TakeProfitConfig(method="fixed_rr", risk_reward=1.5)
    trade_short = simulate_trade(signal, m1_short, None, RiskTracker(config=RiskConfig()), EntryConfig(), StopLossConfig(method="fixed_pips", fixed_pips=15), tp_config, ExecutionConfig(), RiskConfig(), ManagementConfig(breakeven_trigger_r=None))
    trade_long = simulate_trade(signal, m1_long, None, RiskTracker(config=RiskConfig()), EntryConfig(), StopLossConfig(method="fixed_pips", fixed_pips=15), tp_config, ExecutionConfig(), RiskConfig(), ManagementConfig(breakeven_trigger_r=None))

    assert trade_short.exit_price == trade_long.exit_price
    assert trade_short.exit_timestamp == trade_long.exit_timestamp
    assert trade_short.realized_pnl == trade_long.realized_pnl


def test_backtest_deterministic_repeatability():
    m1 = _flat_m1(n=50)
    signal = _signal(direction="bullish", timestamp=m1["timestamp"].iloc[2], entry_zone=(1.0990, 1.1000))
    trades_a = run_backtest([signal], m1, context=None)
    trades_b = run_backtest([signal], m1, context=None)
    assert trades_a[0].to_dict() == trades_b[0].to_dict()
