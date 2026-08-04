"""
Task 10 Phase 8 — Paper Trading Module.

No live broker integration (explicitly out of scope). Instead, this
module replays the decision engine (Phases 1-5) over ALREADY-KNOWN
historical S3/S4 trades from Task 8/9's cached backtest results,
treating each historical trade's pre-entry facts as if they had just
arrived as a "live" signal (see `opportunity.trade_to_opportunity`'s
no-look-ahead guarantee), and separately looks up what actually
happened to compute hypothetical performance.

This directly answers Task 10's central question: would the decision
engine's selectivity have actually improved on a naive "take every
signal" baseline? Both are computed from the SAME historical trades, so
the comparison isolates exactly what the decision engine's ranking/
allocation/risk logic contributes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from src.backtest.trade import TradeStatus
from src.decision_engine.opportunity import trade_to_opportunity
from src.decision_engine.trade_selection import select_trades
from src.decision_engine.risk_layer import AccountState
from src.research.trade_features import FeatureContextIndex


@dataclass
class PaperTradingResult:
    decisions_log: pd.DataFrame
    daily_report: pd.DataFrame
    weekly_report: pd.DataFrame
    selected_summary: dict
    baseline_summary: dict


def _outcome_metrics(trades: list, starting_balance: float = 10_000.0) -> dict:
    closed = [t for t in trades if t is not None and t.status == TradeStatus.CLOSED.value]
    if not closed:
        return {"n": 0, "win_rate": None, "expectancy_r": None, "profit_factor": None, "net_profit": 0.0}
    pnls = [t.realized_pnl for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_profit = sum(wins) if wins else 0.0
    gross_loss = sum(losses) if losses else 0.0
    return {
        "n": len(closed),
        "win_rate": round(len(wins) / len(closed), 4),
        "expectancy_r": round(sum(t.r_multiple for t in closed if t.r_multiple is not None) / len(closed), 4),
        "profit_factor": round(gross_profit / abs(gross_loss), 4) if gross_loss != 0 else (float("inf") if gross_profit > 0 else 0.0),
        "net_profit": round(sum(pnls), 2),
    }


def run_paper_trading(trades_by_symbol: dict, starting_balance: float = 10_000.0) -> PaperTradingResult:
    """trades_by_symbol: {symbol: {"context": MarketContext, "trades_by_strategy": {"S3": [...], "S4": [...]}}}
    -- exactly Task 8's cached per-symbol Tier 1 shape. Walks all S3/S4
    trades across all symbols in chronological order, grouped into daily
    decision cycles (a new cycle starts whenever the calendar date of the
    next trade's entry_timestamp changes)."""
    trade_to_opp = {}
    opp_to_trade = {}
    all_trades = []
    for symbol, res in trades_by_symbol.items():
        index = FeatureContextIndex(res["context"], timeframe="M15")
        for sid in ("S3", "S4"):
            for trade in res["trades_by_strategy"].get(sid, []):
                if trade.entry_timestamp is None:
                    continue
                opp = trade_to_opportunity(trade, index)
                if opp is None:
                    continue
                trade_to_opp[trade.trade_id] = opp
                opp_to_trade[opp.opportunity_id] = trade
                all_trades.append(trade)

    all_trades.sort(key=lambda t: t.entry_timestamp)
    decisions_rows = []
    account = AccountState(starting_balance=starting_balance, balance=starting_balance)
    daily_pnl_by_date: dict = {}

    current_date = None
    batch: list = []

    def _flush(date_key, batch_trades):
        nonlocal account
        if not batch_trades:
            return
        opps = [trade_to_opp[t.trade_id] for t in batch_trades]
        decisions = select_trades(opps, account)
        day_pnl = 0.0
        for d in decisions:
            trade = opp_to_trade[d.opportunity.opportunity_id]
            outcome_pnl = trade.realized_pnl if (d.verdict == "EXECUTE" and trade.status == TradeStatus.CLOSED.value) else 0.0
            day_pnl += outcome_pnl
            decisions_rows.append({
                "date": date_key, "trade_id": trade.trade_id, "symbol": trade.symbol, "strategy_id": trade.strategy_id,
                "entry_timestamp": trade.entry_timestamp, "ios": d.ios, "ios_tier": d.ios_tier, "verdict": d.verdict,
                "reasons_against": "; ".join(d.reasons_against),
                "allocated_risk_pct": d.allocated_risk_pct,
                "actual_status": trade.status, "actual_realized_pnl": trade.realized_pnl if trade.status == TradeStatus.CLOSED.value else None,
                "actual_r_multiple": trade.r_multiple,
                "counted_pnl": outcome_pnl,
            })
        daily_pnl_by_date[date_key] = daily_pnl_by_date.get(date_key, 0.0) + day_pnl
        account.balance += day_pnl
        account.daily_pnl = day_pnl
        account.weekly_pnl = sum(v for k, v in daily_pnl_by_date.items() if (date_key - k).days < 7)
        account.monthly_pnl = sum(v for k, v in daily_pnl_by_date.items() if (date_key - k).days < 30)

    for trade in all_trades:
        d = trade.entry_timestamp.date()
        if current_date is not None and d != current_date:
            _flush(current_date, batch)
            batch = []
        current_date = d
        batch.append(trade)
    _flush(current_date, batch)

    decisions_log = pd.DataFrame(decisions_rows)

    daily_report = decisions_log.groupby("date").agg(
        opportunities=("trade_id", "count"),
        executed=("verdict", lambda s: (s == "EXECUTE").sum()),
        postponed=("verdict", lambda s: (s == "POSTPONE").sum()),
        ignored=("verdict", lambda s: (s == "IGNORE").sum()),
        pnl=("counted_pnl", "sum"),
    ).reset_index()
    daily_report["cumulative_pnl"] = daily_report["pnl"].cumsum()

    weekly = decisions_log.copy()
    weekly["week"] = pd.to_datetime(weekly["date"]).dt.to_period("W").astype(str)
    weekly_report = weekly.groupby("week").agg(
        opportunities=("trade_id", "count"),
        executed=("verdict", lambda s: (s == "EXECUTE").sum()),
        pnl=("counted_pnl", "sum"),
    ).reset_index()
    weekly_report["cumulative_pnl"] = weekly_report["pnl"].cumsum()

    executed_trades = [opp_to_trade[row.trade_id] for row in decisions_log.itertuples() if row.verdict == "EXECUTE"]
    selected_summary = _outcome_metrics(executed_trades, starting_balance)
    baseline_summary = _outcome_metrics(all_trades, starting_balance)

    return PaperTradingResult(
        decisions_log=decisions_log, daily_report=daily_report, weekly_report=weekly_report,
        selected_summary=selected_summary, baseline_summary=baseline_summary,
    )
