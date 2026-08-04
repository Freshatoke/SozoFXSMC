"""Task 10 Phase 9 — Institutional Dashboard: combines the opportunity
queue, IOS rankings, portfolio exposure, risk metrics, and paper-trading
performance into one HTML dashboard."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "reports" / "decision_engine"


def ios_distribution_chart(decisions: pd.DataFrame) -> go.Figure:
    fig = go.Figure()
    for verdict in ["EXECUTE", "POSTPONE", "IGNORE"]:
        sub = decisions[decisions.verdict == verdict]
        fig.add_trace(go.Histogram(x=sub["ios"], name=verdict, opacity=0.7))
    fig.update_layout(title="IOS Distribution by Verdict", template="plotly_white", barmode="overlay")
    return fig


def verdict_by_strategy_chart(decisions: pd.DataFrame) -> go.Figure:
    pivot = decisions.groupby(["strategy_id", "verdict"]).size().unstack(fill_value=0)
    fig = go.Figure()
    for verdict in pivot.columns:
        fig.add_trace(go.Bar(x=pivot.index, y=pivot[verdict], name=verdict))
    fig.update_layout(title="Decisions by Strategy", template="plotly_white", barmode="stack")
    return fig


def currency_exposure_chart(decisions: pd.DataFrame) -> go.Figure:
    executed = decisions[decisions.verdict == "EXECUTE"]
    legs = []
    for sym in executed["symbol"]:
        legs.append(sym[:3])
        legs.append(sym[3:6])
    counts = pd.Series(legs).value_counts()
    fig = go.Figure(data=go.Bar(x=counts.index, y=counts.values))
    fig.update_layout(title="Executed-Trade Currency Exposure", template="plotly_white")
    return fig


def cumulative_pnl_chart(daily_report: pd.DataFrame) -> go.Figure:
    fig = go.Figure(data=go.Scatter(x=daily_report["date"], y=daily_report["cumulative_pnl"], mode="lines"))
    fig.update_layout(title="Paper Trading: Cumulative PnL (Decision-Engine-Selected Trades)", template="plotly_white")
    return fig


def selected_vs_baseline_chart(summary: dict) -> go.Figure:
    metrics = ["win_rate", "expectancy_r", "profit_factor"]
    fig = make_subplots(rows=1, cols=3, subplot_titles=metrics)
    for i, m in enumerate(metrics, start=1):
        fig.add_trace(go.Bar(x=["Selected (Engine)", "Baseline (All)"],
                              y=[summary["selected"][m], summary["baseline"][m]],
                              marker_color=["#2ca02c", "#7f7f7f"]), row=1, col=i)
    fig.update_layout(title="Decision Engine Selection vs. Take-Everything Baseline", template="plotly_white", showlegend=False)
    return fig


def main() -> None:
    decisions = pd.read_parquet(OUT_DIR / "paper_trading_decisions.parquet")
    daily_report = pd.read_csv(OUT_DIR / "paper_trading_daily_report.csv")
    with open(OUT_DIR / "paper_trading_summary.json") as f:
        summary = json.load(f)

    figs = {
        "selected_vs_baseline": selected_vs_baseline_chart(summary),
        "cumulative_pnl": cumulative_pnl_chart(daily_report),
        "ios_distribution": ios_distribution_chart(decisions),
        "verdict_by_strategy": verdict_by_strategy_chart(decisions),
        "currency_exposure": currency_exposure_chart(decisions),
    }

    executed = decisions[decisions.verdict == "EXECUTE"].sort_values("ios", ascending=False).head(20)
    rejected = decisions[decisions.verdict.isin(["IGNORE", "POSTPONE"])].sort_values("ios", ascending=False).head(20)

    parts = ["<html><head><title>Institutional Trading Decision Engine Dashboard</title></head><body>",
             "<h1>Forex SMC Quant -- Institutional Trading Decision Engine (ITDE) Dashboard</h1>",
             f"<p>Selected: {summary['selected']} </p><p>Baseline: {summary['baseline']}</p>"]
    for name, fig in figs.items():
        parts.append(f"<h2>{name.replace('_', ' ').title()}</h2>")
        parts.append(fig.to_html(full_html=False, include_plotlyjs="cdn"))

    parts.append("<h2>Top 20 Executed Opportunities (by IOS)</h2>")
    parts.append(executed[["date", "symbol", "strategy_id", "ios", "ios_tier", "allocated_risk_pct", "actual_realized_pnl"]].to_html(index=False))
    parts.append("<h2>Top 20 Rejected/Postponed Opportunities (by IOS)</h2>")
    parts.append(rejected[["date", "symbol", "strategy_id", "ios", "verdict", "reasons_against"]].to_html(index=False))
    parts.append("</body></html>")

    (OUT_DIR / "institutional_dashboard.html").write_text("\n".join(parts), encoding="utf-8")
    print(f"Wrote {OUT_DIR / 'institutional_dashboard.html'}")


if __name__ == "__main__":
    main()
