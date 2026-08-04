"""
Filter Effectiveness: for any named boolean predicate over a `Trade`,
compares "With" vs "Without" performance and reports a verdict
(improves/reduces/neutral expectancy). Built-in predicates cover the
task brief's examples (FVG, Engulfing, Liquidity Sweep, gap-size
thresholds); Order Block freshness needs a `MarketContext` to look up
each trade's referenced OB by id (see `fresh_ob_predicate`).

This is pure post-hoc RESEARCH REPORTING over already-closed historical
trades -- it explains outcomes, it does not gate any future decision, so
using each Order Block's final (whole-history) freshness/mitigation
outcome here is not look-ahead bias (that concern only applies to
decisions made *during* signal/trade generation, which this module never
touches).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.backtest.trade import TradeStatus
from src.backtest.metrics import build_equity_curve, max_drawdown


def _reason_code_predicate(*substrings):
    def predicate(t):
        return any(any(s in code for s in substrings) for code in (t.reason_codes or []))
    return predicate


def has_fvg(t) -> bool:
    return _reason_code_predicate("FVG")(t)


def has_engulfing(t) -> bool:
    return _reason_code_predicate("Engulfing")(t)


def has_liquidity_sweep(t) -> bool:
    return _reason_code_predicate("Swept")(t)


def gap_above_pips(pips: float, pip_size: float = 0.0001):
    def predicate(t):
        gap_size = t.metadata.get("gap_size")
        return gap_size is not None and abs(gap_size) >= pips * pip_size
    return predicate


def fresh_ob_predicate(context, timeframe: str = "M15"):
    """Requires a MarketContext already built for the same market data.
    Looks up each trade's referenced OB (via confluence_snapshot["order_block_id"])
    and checks whether it was NEVER touched across the whole dataset
    (freshness_status == "FRESH") vs. touched/mitigated at some point."""
    obs = context.order_blocks(timeframe)
    by_id = {row["ob_id"]: row for _, row in obs.iterrows()} if not obs.empty else {}

    def predicate(t):
        ob_id = (t.confluence_snapshot or {}).get("order_block_id")
        if ob_id is None or ob_id not in by_id:
            return None  # unknown -- excluded from the comparison, not counted as False
        return by_id[ob_id]["freshness_status"] == "FRESH"
    return predicate


def _metrics_for(trades: list, starting_balance: float) -> dict:
    closed = [t for t in trades if t.status == TradeStatus.CLOSED.value]
    pnls = [t.realized_pnl for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_profit, gross_loss = float(np.sum(wins)) if wins else 0.0, float(np.sum(losses)) if losses else 0.0
    curve = build_equity_curve(closed, starting_balance)
    dd = max_drawdown(curve)
    return {
        "num_trades": len(closed),
        "win_rate": round(len(wins) / len(closed), 4) if closed else 0.0,
        "profit_factor": round(gross_profit / abs(gross_loss), 4) if gross_loss != 0 else (float("inf") if gross_profit > 0 else 0.0),
        "expectancy": round(float(np.mean(pnls)), 4) if pnls else 0.0,
        "max_drawdown_pct": round(dd["max_drawdown_pct"], 4),
    }


def evaluate_filter(trades: list, predicate, name: str, starting_balance: float = 10_000.0) -> pd.DataFrame:
    with_trades, without_trades = [], []
    for t in trades:
        result = predicate(t)
        if result is None:
            continue
        (with_trades if result else without_trades).append(t)

    with_metrics = _metrics_for(with_trades, starting_balance)
    without_metrics = _metrics_for(without_trades, starting_balance)
    rows = [{"filter": name, "group": "With", **with_metrics}, {"filter": name, "group": "Without", **without_metrics}]
    return pd.DataFrame(rows)


def compare_filters(trades: list, filters: dict, starting_balance: float = 10_000.0) -> pd.DataFrame:
    """filters: {name: predicate}. Returns one row per filter with the
    expectancy delta (With - Without) and a verdict."""
    rows = []
    for name, predicate in filters.items():
        table = evaluate_filter(trades, predicate, name, starting_balance)
        with_row = table[table.group == "With"].iloc[0]
        without_row = table[table.group == "Without"].iloc[0]
        delta = with_row["expectancy"] - without_row["expectancy"]
        verdict = "improves" if delta > 0 else ("reduces" if delta < 0 else "neutral")
        rows.append({
            "filter": name,
            "with_num_trades": with_row["num_trades"], "without_num_trades": without_row["num_trades"],
            "with_expectancy": with_row["expectancy"], "without_expectancy": without_row["expectancy"],
            "expectancy_delta": round(delta, 4),
            "with_profit_factor": with_row["profit_factor"], "without_profit_factor": without_row["profit_factor"],
            "verdict": verdict,
        })
    return pd.DataFrame(rows)
