"""Shared grouped-metrics helper used by symbol/session/confidence/filter
analysis so each of those modules doesn't reimplement the same
aggregation (num_trades, win_rate, profit_factor, expectancy, avg R,
max drawdown %) over its own grouping key."""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.backtest.trade import TradeStatus
from src.backtest.metrics import build_equity_curve, max_drawdown


def group_metrics(trades: list, key_fn, starting_balance: float = 10_000.0) -> pd.DataFrame:
    closed = [t for t in trades if t.status == TradeStatus.CLOSED.value]
    groups: dict = {}
    for t in closed:
        groups.setdefault(key_fn(t), []).append(t)

    rows = []
    for key, group_trades in groups.items():
        pnls = [t.realized_pnl for t in group_trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        r_values = [t.r_multiple for t in group_trades if t.r_multiple is not None]
        gross_profit = float(np.sum(wins)) if wins else 0.0
        gross_loss = float(np.sum(losses)) if losses else 0.0
        curve = build_equity_curve(group_trades, starting_balance)
        dd = max_drawdown(curve)

        rows.append({
            "group": key,
            "num_trades": len(group_trades),
            "win_rate": round(len(wins) / len(group_trades), 4) if group_trades else 0.0,
            "profit_factor": round(gross_profit / abs(gross_loss), 4) if gross_loss != 0 else (float("inf") if gross_profit > 0 else 0.0),
            "expectancy": round(float(np.mean(pnls)), 4) if pnls else 0.0,
            "average_r": round(float(np.mean(r_values)), 4) if r_values else 0.0,
            "net_profit": round(float(np.sum(pnls)), 4),
            "max_drawdown_pct": round(dd["max_drawdown_pct"], 4),
        })
    return pd.DataFrame(rows).sort_values("expectancy", ascending=False).reset_index(drop=True) if rows else pd.DataFrame(
        columns=["group", "num_trades", "win_rate", "profit_factor", "expectancy", "average_r", "net_profit", "max_drawdown_pct"]
    )
