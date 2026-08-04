"""
Performance Metrics: turns a list of closed `Trade` objects into the full
institutional-grade metric suite required by the task brief, plus an
equity curve / drawdown series and expectancy breakdowns by strategy,
symbol, session, direction, and confidence bucket.

Only CLOSED trades count toward performance metrics. EXPIRED and
REJECTED trades are reported separately as "signal utilization" stats --
they represent signals that never became live risk, which is itself
useful information (e.g. a strategy whose signals are mostly REJECTED by
risk limits needs looser limits or fewer simultaneous signals, not a
"better" strategy).
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from src.backtest.trade import Trade, TradeStatus


def _closed(trades: list) -> list:
    return [t for t in trades if t.status == TradeStatus.CLOSED.value]


def build_equity_curve(trades: list, starting_balance: float) -> pd.DataFrame:
    closed = sorted(_closed(trades), key=lambda t: t.exit_timestamp)
    rows = [{"timestamp": None, "balance": starting_balance, "trade_id": None}]
    balance = starting_balance
    for t in closed:
        balance += t.realized_pnl
        rows.append({"timestamp": t.exit_timestamp, "balance": balance, "trade_id": t.trade_id})
    curve = pd.DataFrame(rows)
    curve["peak"] = curve["balance"].cummax()
    curve["drawdown"] = curve["balance"] - curve["peak"]
    curve["drawdown_pct"] = curve["drawdown"] / curve["peak"]
    return curve


def max_drawdown(equity_curve: pd.DataFrame) -> dict:
    if equity_curve.empty:
        return {"max_drawdown": 0.0, "max_drawdown_pct": 0.0}
    return {
        "max_drawdown": float(equity_curve["drawdown"].min()),
        "max_drawdown_pct": float(equity_curve["drawdown_pct"].min()),
    }


def _max_consecutive(results: list, predicate) -> int:
    best = current = 0
    for r in results:
        if predicate(r):
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


def _sharpe_sortino(closed: list, starting_balance: float) -> dict:
    if not closed:
        return {"sharpe_ratio": 0.0, "sortino_ratio": 0.0}
    daily = {}
    for t in closed:
        day = t.exit_timestamp.date()
        daily[day] = daily.get(day, 0.0) + t.realized_pnl
    days = sorted(daily.keys())
    balance = starting_balance
    returns = []
    for day in days:
        ret = daily[day] / balance if balance > 0 else 0.0
        returns.append(ret)
        balance += daily[day]
    returns = np.array(returns)
    if len(returns) < 2 or returns.std(ddof=1) == 0:
        sharpe = 0.0
    else:
        sharpe = float(returns.mean() / returns.std(ddof=1) * math.sqrt(252))
    downside = returns[returns < 0]
    if len(downside) < 2 or downside.std(ddof=1) == 0:
        sortino = 0.0
    else:
        sortino = float(returns.mean() / downside.std(ddof=1) * math.sqrt(252))
    return {"sharpe_ratio": round(sharpe, 4), "sortino_ratio": round(sortino, 4)}


def _expectancy_by(closed: list, key_fn) -> dict:
    groups: dict = {}
    for t in closed:
        key = key_fn(t)
        groups.setdefault(key, []).append(t)
    out = {}
    for key, ts in groups.items():
        pnls = [t.realized_pnl for t in ts]
        wins = [p for p in pnls if p > 0]
        out[str(key)] = {
            "num_trades": len(ts),
            "win_rate": round(len(wins) / len(ts), 4) if ts else 0.0,
            "expectancy": round(float(np.mean(pnls)), 4) if pnls else 0.0,
            "net_profit": round(float(np.sum(pnls)), 4),
        }
    return out


def _confidence_bucket(score: float) -> str:
    lower = int(score // 10) * 10
    return f"{lower}-{lower + 10}"


def compute_performance_metrics(trades: list, starting_balance: float) -> dict:
    closed = _closed(trades)
    expired = [t for t in trades if t.status == TradeStatus.EXPIRED.value]
    rejected = [t for t in trades if t.status == TradeStatus.REJECTED.value]

    pnls = [t.realized_pnl for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    equity_curve = build_equity_curve(trades, starting_balance)
    dd = max_drawdown(equity_curve)

    win_flags = [t.realized_pnl > 0 for t in closed]
    r_multiples = [t.r_multiple for t in closed if t.r_multiple is not None]

    gross_profit = float(np.sum(wins)) if wins else 0.0
    gross_loss = float(np.sum(losses)) if losses else 0.0
    net_profit = gross_profit + gross_loss

    metrics = {
        "signal_utilization": {
            "total_signals": len(trades),
            "closed_trades": len(closed),
            "expired": len(expired),
            "rejected": len(rejected),
            "rejection_reasons": _expectancy_by(rejected, lambda t: t.rejection_reason) if rejected else {},
        },
        "net_profit": round(net_profit, 4),
        "gross_profit": round(gross_profit, 4),
        "gross_loss": round(gross_loss, 4),
        "win_rate": round(len(wins) / len(closed), 4) if closed else 0.0,
        "loss_rate": round(len(losses) / len(closed), 4) if closed else 0.0,
        "profit_factor": round(gross_profit / abs(gross_loss), 4) if gross_loss != 0 else (float("inf") if gross_profit > 0 else 0.0),
        "expectancy": round(float(np.mean(pnls)), 4) if pnls else 0.0,
        "average_winner": round(float(np.mean(wins)), 4) if wins else 0.0,
        "average_loser": round(float(np.mean(losses)), 4) if losses else 0.0,
        "max_drawdown": round(dd["max_drawdown"], 4),
        "max_drawdown_pct": round(dd["max_drawdown_pct"], 4),
        "max_consecutive_wins": _max_consecutive(win_flags, lambda w: w),
        "max_consecutive_losses": _max_consecutive(win_flags, lambda w: not w),
        "average_trade_duration_candles": round(float(np.mean([t.duration_candles for t in closed])), 2) if closed else 0.0,
        "average_mae": round(float(np.mean([t.mae for t in closed])), 6) if closed else 0.0,
        "average_mfe": round(float(np.mean([t.mfe for t in closed])), 6) if closed else 0.0,
        "r_multiple_distribution": {
            "mean": round(float(np.mean(r_multiples)), 4) if r_multiples else 0.0,
            "std": round(float(np.std(r_multiples, ddof=1)), 4) if len(r_multiples) > 1 else 0.0,
            "min": round(float(np.min(r_multiples)), 4) if r_multiples else 0.0,
            "max": round(float(np.max(r_multiples)), 4) if r_multiples else 0.0,
            "values": r_multiples,
        },
        "recovery_factor": round(net_profit / abs(dd["max_drawdown"]), 4) if dd["max_drawdown"] != 0 else (float("inf") if net_profit > 0 else 0.0),
        "expectancy_by_strategy": _expectancy_by(closed, lambda t: t.strategy_id),
        "expectancy_by_symbol": _expectancy_by(closed, lambda t: t.symbol),
        "expectancy_by_session": _expectancy_by(closed, lambda t: t.session or "unknown"),
        "expectancy_by_direction": _expectancy_by(closed, lambda t: t.direction),
        "expectancy_by_confidence": _expectancy_by(closed, lambda t: _confidence_bucket(t.confidence_score)),
    }
    metrics.update(_sharpe_sortino(closed, starting_balance))
    return metrics


def trades_to_dataframe(trades: list) -> pd.DataFrame:
    return pd.DataFrame([t.to_dict() for t in trades])
