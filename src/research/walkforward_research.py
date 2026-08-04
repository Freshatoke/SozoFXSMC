"""
Walk-Forward Research: extends `src.backtest.walkforward`'s single
train/validation/out-of-sample split with ROLLING windows -- a sequence
of test periods stepping forward through the dataset -- and reports each
window's performance SEPARATELY, so a strategy's stability over time can
be judged directly rather than inferred from one aggregate backtest.

Parameter optimisation is still out of scope (per the task brief): these
windows are for evaluating a FIXED configuration's performance
consistency over time, not for fitting parameters per window. A future
optimisation task would fit on each window's train period and validate
on its test period; this module already produces the window boundaries
that task will need.

Trades are never re-simulated per window -- `run_backtest` is run ONCE
over the full dataset (so market structure near a window boundary still
has the lookback context it needs), and each window simply FILTERS the
resulting trades to those whose `entry_timestamp` falls in that window's
test range. This is both faster and more correct than re-slicing the M1
data per window, which would starve early trades in each window of the
structural history (swings, OBs, etc.) they depend on.
"""

from __future__ import annotations

import pandas as pd

from src.backtest.trade import TradeStatus
from src.backtest.metrics import compute_performance_metrics


def generate_rolling_windows(m1: pd.DataFrame, test_days: int = 5, step_days: int = 5, train_days: int = 20) -> list:
    """Returns a list of {"window_id", "train_start", "train_end",
    "test_start", "test_end"} dicts stepping forward through the dataset.
    `train_start`/`train_end` are informational (the period a future
    optimisation task would fit on); this module does not use them for
    anything itself."""
    if m1.empty:
        return []
    start, end = m1["timestamp"].iloc[0], m1["timestamp"].iloc[-1]
    windows = []
    window_id = 0
    test_start = start + pd.Timedelta(days=train_days)
    while test_start < end:
        test_end = min(test_start + pd.Timedelta(days=test_days), end)
        windows.append({
            "window_id": window_id,
            "train_start": test_start - pd.Timedelta(days=train_days),
            "train_end": test_start,
            "test_start": test_start,
            "test_end": test_end,
        })
        window_id += 1
        test_start = test_start + pd.Timedelta(days=step_days)
    return windows


def evaluate_rolling_windows(trades: list, windows: list, starting_balance: float = 10_000.0) -> pd.DataFrame:
    rows = []
    for w in windows:
        window_trades = [
            t for t in trades
            if t.status == TradeStatus.CLOSED.value and t.entry_timestamp is not None
            and w["test_start"] <= t.entry_timestamp < w["test_end"]
        ]
        metrics = compute_performance_metrics(window_trades, starting_balance)
        rows.append({
            "window_id": w["window_id"], "test_start": w["test_start"], "test_end": w["test_end"],
            "num_trades": metrics["signal_utilization"]["closed_trades"],
            "win_rate": metrics["win_rate"], "profit_factor": metrics["profit_factor"],
            "expectancy": metrics["expectancy"], "net_profit": metrics["net_profit"],
            "max_drawdown_pct": metrics["max_drawdown_pct"],
        })
    return pd.DataFrame(rows)


def summarize_stability(rolling_results: pd.DataFrame) -> dict:
    """A quick stability read: how consistent is expectancy across
    windows? High variance / sign-flipping expectancy across windows is a
    red flag that the aggregate backtest number is not representative."""
    if rolling_results.empty:
        return {}
    positive_windows = int((rolling_results["expectancy"] > 0).sum())
    return {
        "num_windows": len(rolling_results),
        "positive_expectancy_windows": positive_windows,
        "positive_window_pct": round(positive_windows / len(rolling_results), 4),
        "expectancy_mean": round(float(rolling_results["expectancy"].mean()), 4),
        "expectancy_std": round(float(rolling_results["expectancy"].std(ddof=1)), 4) if len(rolling_results) > 1 else 0.0,
    }
