"""
Task 11 Phase 11 — Analytics.

Generates a report from an `LiveOrchestrator`'s current state (or from a
saved event log alone, via `generate_report_from_events`, for offline
analysis after a run has ended). One function produces every metric the
task brief names: win rate, expectancy, IOS distribution, trade
distribution, risk utilization, system uptime, latency, data quality,
provider health. Called "daily/weekly/monthly" per the brief, but the
function itself is period-agnostic -- it reports on whatever trades and
events it's given; the CALLER (a scheduled job in real deployment, or
`scripts/run_forward_paper_trading.py`'s own summary here) decides what
date range that is. This run's own report only covers the single bounded
demo session, which is exactly the "period" available to report on --
see docs/PRODUCTION_READINESS_REPORT_TASK11.md for why a real
daily/weekly/monthly cadence cannot yet be demonstrated.
"""

from __future__ import annotations

from src.decision_engine.portfolio_allocation import ios_tier


def _win_rate_and_expectancy(closed_positions: list) -> dict:
    if not closed_positions:
        return {"trade_count": 0, "win_rate": None, "expectancy_currency": None, "gross_pnl": 0.0}
    wins = [p for p in closed_positions if p.realized_pnl > 0]
    return {
        "trade_count": len(closed_positions),
        "win_rate": round(len(wins) / len(closed_positions), 4),
        "expectancy_currency": round(sum(p.realized_pnl for p in closed_positions) / len(closed_positions), 2),
        "gross_pnl": round(sum(p.realized_pnl for p in closed_positions), 2),
    }


def _ios_distribution(approved_log: list, rejected_log: list) -> dict:
    all_decisions = approved_log + rejected_log
    if not all_decisions:
        return {"A": 0, "B": 0, "C": 0, "D": 0}
    dist = {"A": 0, "B": 0, "C": 0, "D": 0}
    for d in all_decisions:
        dist[ios_tier(d.ios)] += 1
    return dist


def _trade_distribution(closed_positions: list, open_positions) -> dict:
    all_positions = list(closed_positions) + list(open_positions.values() if hasattr(open_positions, "values") else open_positions)
    by_symbol, by_strategy = {}, {}
    for p in all_positions:
        by_symbol[p.symbol] = by_symbol.get(p.symbol, 0) + 1
        by_strategy[p.strategy_id] = by_strategy.get(p.strategy_id, 0) + 1
    return {"by_symbol": by_symbol, "by_strategy": by_strategy, "total": len(all_positions)}


def _data_quality(feed_manager) -> dict:
    out = {}
    for symbol, state in feed_manager.symbol_states.items():
        out[symbol] = {
            "total_candles_received": state.total_candles_received,
            "total_gaps_detected": state.total_gaps_detected,
            "total_gap_candles_recovered": state.total_gap_candles_recovered,
            "gap_recovery_rate": (round(state.total_gap_candles_recovered / state.total_gaps_detected, 4)
                                   if state.total_gaps_detected else None),
        }
    return out


def generate_report(orchestrator) -> dict:
    """orchestrator: a `LiveOrchestrator` instance (running or just
    finished -- reads current state, mutates nothing)."""
    broker = orchestrator.broker
    decision_engine = orchestrator.decision_engine
    feed_manager = orchestrator.feed_manager

    import pandas as pd
    now = pd.Timestamp.now(tz="UTC")
    uptime_seconds = (now - orchestrator.started_at).total_seconds()

    hb = feed_manager.provider.heartbeat()

    return {
        "generated_at": str(now),
        "period_start": str(orchestrator.started_at),
        "period_end": str(now),
        "system_uptime_seconds": round(uptime_seconds, 1),
        "cycles_run": orchestrator.cycles_run,
        "performance": _win_rate_and_expectancy(broker.closed_positions),
        "ios_distribution": _ios_distribution(decision_engine.approved_log, decision_engine.rejected_log),
        "trade_distribution": _trade_distribution(broker.closed_positions, broker.open_positions),
        "risk_utilization": {
            "current_portfolio_heat_pct": decision_engine.portfolio_heat_pct,
            "max_portfolio_heat_pct": decision_engine.allocation_limits.max_portfolio_risk_pct,
            "utilization_pct": (round(decision_engine.portfolio_heat_pct / decision_engine.allocation_limits.max_portfolio_risk_pct * 100, 1)
                                 if decision_engine.allocation_limits.max_portfolio_risk_pct else None),
        },
        "decision_summary": {
            "total_opportunities_seen": len(decision_engine.approved_log) + len(decision_engine.rejected_log) + len(decision_engine.postponed_queue),
            "approved": len(decision_engine.approved_log),
            "rejected": len(decision_engine.rejected_log),
            "postponed_pending": len(decision_engine.postponed_queue),
        },
        "data_quality": _data_quality(feed_manager),
        "provider_health": {
            "provider": hb.provider_name, "connected": hb.connected,
            "consecutive_failures": hb.consecutive_failures,
            "last_successful_poll": str(hb.last_successful_poll) if hb.last_successful_poll else None,
            "last_error": hb.last_error,
        },
        "latency": {
            "note": "Dukascopy is a polling near-live source (hourly archive), not a streaming feed -- "
                     "per-tick/per-candle network latency is not a meaningful metric here. The measurable "
                     "latency is per-cycle wall-clock time, which the orchestrator's own run_cycle() timing "
                     "(printed by the demo script) already reports.",
        },
        "total_events_logged": len(orchestrator.event_logger.read_all()),
    }


def render_markdown(report: dict) -> str:
    perf = report["performance"]
    risk = report["risk_utilization"]
    dq = report["data_quality"]
    ph = report["provider_health"]
    lines = [
        f"# Forward Paper Trading Report",
        f"Generated: {report['generated_at']}",
        f"Period: {report['period_start']} -> {report['period_end']} ({report['system_uptime_seconds']}s, {report['cycles_run']} cycles)",
        "",
        "## Performance",
        f"- Trades closed: {perf['trade_count']}",
        f"- Win rate: {perf['win_rate']}",
        f"- Expectancy (currency/trade): {perf['expectancy_currency']}",
        f"- Gross PnL: {perf['gross_pnl']}",
        "",
        "## IOS Distribution",
        f"- {report['ios_distribution']}",
        "",
        "## Trade Distribution",
        f"- {report['trade_distribution']}",
        "",
        "## Risk Utilization",
        f"- Portfolio heat: {risk['current_portfolio_heat_pct']}% / {risk['max_portfolio_heat_pct']}% ({risk['utilization_pct']}%)",
        "",
        "## Decisions",
        f"- {report['decision_summary']}",
        "",
        "## Data Quality",
    ]
    for symbol, q in dq.items():
        lines.append(f"- {symbol}: {q}")
    lines += [
        "",
        "## Provider Health",
        f"- {ph}",
        "",
        f"Total events logged: {report['total_events_logged']}",
    ]
    return "\n".join(lines)
