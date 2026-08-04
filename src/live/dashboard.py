"""
Task 11 Phase 8 — Monitoring Dashboard.

A snapshot-based dashboard, not a persistent web server: `build_snapshot`
pulls current state from every live component (no component needs to
know the dashboard exists) into one flat dict, and `render_html` turns
that into a self-contained HTML file the operator reloads in a browser.
The orchestrator (Phase 12's wiring) calls `write_dashboard` once per
cycle, so "real-time" here means "as fresh as the last completed
candle", the same cadence every other live module already operates on --
there is no separate polling loop to keep the dashboard honest.

Chosen over a live server (Flask/websockets) because Task 11's actual
deliverable is a working forward-paper-trading platform proven over a
short bounded demo (see Task 11 Phase 12's Production Readiness Report
for why weeks-long unattended operation isn't claimed) -- a background
web server outliving the session would be relied upon by no one and
would need infrastructure this environment can't provide continuously.
"""

from __future__ import annotations

import platform
from pathlib import Path
from typing import Optional

import pandas as pd
import psutil


def build_snapshot(feed_manager=None, contexts: Optional[dict] = None, decision_engine=None,
                    broker=None, event_logger=None, started_at: Optional[pd.Timestamp] = None) -> dict:
    """Every argument is optional so the dashboard degrades gracefully if
    a component isn't wired up yet (e.g. during Phase 8's own standalone
    testing, before the orchestrator exists)."""
    now = pd.Timestamp.now(tz="UTC")
    contexts = contexts or {}

    providers = []
    if feed_manager is not None:
        hb = feed_manager.provider.heartbeat()
        providers.append({
            "provider": hb.provider_name, "connected": hb.connected,
            "last_successful_poll": str(hb.last_successful_poll) if hb.last_successful_poll else None,
            "consecutive_failures": hb.consecutive_failures, "last_error": hb.last_error,
        })

    latest_candles = {}
    market_status = {}
    for symbol, ctx in contexts.items():
        m1 = ctx.m1
        if not m1.empty:
            last = m1.iloc[-1]
            latest_candles[symbol] = {
                "timestamp": str(last["timestamp"]), "open": last["open"], "high": last["high"],
                "low": last["low"], "close": last["close"],
            }
            staleness_min = (now - pd.Timestamp(last["timestamp"])).total_seconds() / 60.0
            market_status[symbol] = "STALE" if staleness_min > 5 else "LIVE"
        else:
            market_status[symbol] = "NO_DATA"

    open_trades, pending_trades, portfolio = [], [], {}
    if broker is not None:
        open_trades = [
            {"position_id": p.position_id, "symbol": p.symbol, "direction": p.direction, "entry_price": p.entry_price,
             "remaining_lots": p.remaining_lots, "current_stop_loss": p.current_stop_loss,
             "realized_pnl": p.realized_pnl, "opened_at": str(p.opened_at)}
            for p in broker.open_positions.values()
        ]
        pending_trades = [
            {"order_id": o.order_id, "symbol": o.symbol, "order_type": o.order_type, "trigger_price": o.trigger_price}
            for o in broker.pending_orders.values()
        ]
        portfolio = {
            "balance": round(broker.balance, 2),
            "open_position_count": len(broker.open_positions),
            "closed_position_count": len(broker.closed_positions),
            "total_realized_pnl": round(sum(p.realized_pnl for p in broker.closed_positions), 2),
        }

    rejected_opportunities, risk = [], {}
    if decision_engine is not None:
        rejected_opportunities = [
            {"opportunity_id": d.opportunity.opportunity_id, "symbol": d.opportunity.symbol,
             "reasons": d.reasons_against}
            for d in decision_engine.rejected_log[-20:]
        ]
        risk = {
            "portfolio_heat_pct": decision_engine.portfolio_heat_pct,
            "currency_exposure": decision_engine.currency_exposure,
            "strategy_exposure": decision_engine.strategy_exposure,
            "daily_pnl": decision_engine.account.daily_pnl,
            "max_daily_loss_pct": decision_engine.risk_limits.max_daily_loss_pct,
            "cycles_run": decision_engine.cycles_run,
            "open_portfolio_count": len(decision_engine.open_portfolio),
            "postponed_queue_count": len(decision_engine.postponed_queue),
        }

    event_count = len(event_logger.read_all()) if event_logger is not None else 0

    uptime_seconds = (now - started_at).total_seconds() if started_at is not None else None
    system_health = {
        "cpu_percent": psutil.cpu_percent(interval=None),
        "memory_percent": psutil.virtual_memory().percent,
        "memory_used_mb": round(psutil.Process().memory_info().rss / (1024 * 1024), 1),
        "platform": platform.platform(),
        "uptime_seconds": round(uptime_seconds, 1) if uptime_seconds is not None else None,
    }

    return {
        "generated_at": str(now),
        "providers": providers,
        "latest_candles": latest_candles,
        "market_status": market_status,
        "open_trades": open_trades,
        "pending_trades": pending_trades,
        "rejected_opportunities": rejected_opportunities,
        "portfolio": portfolio,
        "risk": risk,
        "system_health": system_health,
        "total_events_logged": event_count,
    }


def _rows(items: list, columns: list) -> str:
    if not items:
        return "<tr><td colspan='99' class='empty'>None</td></tr>"
    out = []
    for item in items:
        cells = "".join(f"<td>{item.get(c, '')}</td>" for c in columns)
        out.append(f"<tr>{cells}</tr>")
    return "".join(out)


def render_html(snapshot: dict) -> str:
    sh = snapshot["system_health"]
    portfolio = snapshot["portfolio"]
    risk = snapshot["risk"]
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>forex-smc-quant — Live Monitoring</title>
<style>
body {{ font-family: -apple-system, Segoe UI, sans-serif; background: #0d1117; color: #c9d1d9; padding: 20px; }}
h1 {{ color: #58a6ff; }} h2 {{ color: #79c0ff; border-bottom: 1px solid #30363d; padding-bottom: 4px; margin-top: 28px; }}
table {{ width: 100%; border-collapse: collapse; margin-top: 8px; }}
th, td {{ text-align: left; padding: 6px 10px; border-bottom: 1px solid #21262d; font-size: 13px; }}
th {{ color: #8b949e; text-transform: uppercase; font-size: 11px; }}
.empty {{ color: #6e7681; font-style: italic; }}
.kv {{ display: flex; flex-wrap: wrap; gap: 16px; }}
.kv div {{ background: #161b22; border: 1px solid #30363d; border-radius: 6px; padding: 10px 16px; }}
.kv .label {{ color: #8b949e; font-size: 11px; text-transform: uppercase; }}
.kv .value {{ font-size: 18px; font-weight: 600; color: #e6edf3; }}
.LIVE {{ color: #3fb950; }} .STALE {{ color: #d29922; }} .NO_DATA {{ color: #f85149; }}
</style></head>
<body>
<h1>forex-smc-quant — Live Monitoring</h1>
<p>Generated: {snapshot['generated_at']} &middot; Total events logged: {snapshot['total_events_logged']}</p>

<h2>System Health</h2>
<div class="kv">
  <div><div class="label">CPU</div><div class="value">{sh['cpu_percent']}%</div></div>
  <div><div class="label">Memory</div><div class="value">{sh['memory_percent']}% ({sh['memory_used_mb']} MB)</div></div>
  <div><div class="label">Uptime</div><div class="value">{sh['uptime_seconds']}s</div></div>
  <div><div class="label">Platform</div><div class="value" style="font-size:12px">{sh['platform']}</div></div>
</div>

<h2>Connected Providers</h2>
<table><tr><th>Provider</th><th>Connected</th><th>Last Poll</th><th>Consecutive Failures</th><th>Last Error</th></tr>
{_rows(snapshot['providers'], ['provider', 'connected', 'last_successful_poll', 'consecutive_failures', 'last_error'])}
</table>

<h2>Market Status &amp; Latest Candles</h2>
<table><tr><th>Symbol</th><th>Status</th><th>Timestamp</th><th>O</th><th>H</th><th>L</th><th>C</th></tr>
{''.join(f"<tr><td>{sym}</td><td class='{snapshot['market_status'].get(sym,'')}'>{snapshot['market_status'].get(sym,'')}</td>"
         f"<td>{c['timestamp']}</td><td>{c['open']}</td><td>{c['high']}</td><td>{c['low']}</td><td>{c['close']}</td></tr>"
         for sym, c in snapshot['latest_candles'].items()) or "<tr><td colspan='7' class='empty'>None</td></tr>"}
</table>

<h2>Open Trades</h2>
<table><tr><th>Position</th><th>Symbol</th><th>Dir</th><th>Entry</th><th>Lots</th><th>Stop</th><th>Realized PnL</th><th>Opened</th></tr>
{_rows(snapshot['open_trades'], ['position_id', 'symbol', 'direction', 'entry_price', 'remaining_lots', 'current_stop_loss', 'realized_pnl', 'opened_at'])}
</table>

<h2>Pending Trades</h2>
<table><tr><th>Order</th><th>Symbol</th><th>Type</th><th>Trigger</th></tr>
{_rows(snapshot['pending_trades'], ['order_id', 'symbol', 'order_type', 'trigger_price'])}
</table>

<h2>Recently Rejected Opportunities</h2>
<table><tr><th>Opportunity</th><th>Symbol</th><th>Reasons</th></tr>
{_rows(snapshot['rejected_opportunities'], ['opportunity_id', 'symbol', 'reasons'])}
</table>

<h2>Portfolio &amp; PnL</h2>
<div class="kv">
  <div><div class="label">Balance</div><div class="value">{portfolio.get('balance', '-')}</div></div>
  <div><div class="label">Open Positions</div><div class="value">{portfolio.get('open_position_count', '-')}</div></div>
  <div><div class="label">Closed Positions</div><div class="value">{portfolio.get('closed_position_count', '-')}</div></div>
  <div><div class="label">Total Realized PnL</div><div class="value">{portfolio.get('total_realized_pnl', '-')}</div></div>
</div>

<h2>Risk</h2>
<div class="kv">
  <div><div class="label">Portfolio Heat</div><div class="value">{risk.get('portfolio_heat_pct', '-')}%</div></div>
  <div><div class="label">Daily PnL</div><div class="value">{risk.get('daily_pnl', '-')}</div></div>
  <div><div class="label">Decision Cycles</div><div class="value">{risk.get('cycles_run', '-')}</div></div>
  <div><div class="label">Currency Exposure</div><div class="value" style="font-size:13px">{risk.get('currency_exposure', {})}</div></div>
  <div><div class="label">Strategy Exposure</div><div class="value" style="font-size:13px">{risk.get('strategy_exposure', {})}</div></div>
</div>
</body></html>"""


def write_dashboard(path: str | Path, snapshot: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render_html(snapshot), encoding="utf-8")
