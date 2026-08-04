"""Task 9 — Research Dashboard: combines every Phase 1-10 output into one
HTML dashboard, following the same pattern as Task 8's research_dashboard.html."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "reports" / "edge_refinement"


def feature_importance_chart() -> go.Figure:
    df = pd.read_csv(OUT_DIR / "feature_importance.csv")
    fig = go.Figure()
    for sid in ["S3", "S4"]:
        sub = df[df.strategy_id == sid].sort_values("mutual_info_vs_win", ascending=False).head(10)
        fig.add_trace(go.Bar(x=sub["feature"], y=sub["mutual_info_vs_win"], name=sid))
    fig.update_layout(title="Top 10 Features by Mutual Information vs. Win", template="plotly_white", barmode="group")
    return fig


def confluence_chart() -> go.Figure:
    df = pd.read_csv(OUT_DIR / "confluence_report.csv")
    df = df[df.n >= 10]
    fig = go.Figure()
    for sid in ["S3", "S4"]:
        sub = df[df.strategy_id == sid]
        fig.add_trace(go.Bar(x=sub["combination"], y=sub["expectancy_r"], name=sid, text=sub["n"]))
    fig.update_layout(title="Confluence Combinations: Expectancy (R), n>=10 only", template="plotly_white", xaxis_tickangle=-45, barmode="group")
    return fig


def itqs_bucket_chart() -> go.Figure:
    df = pd.read_csv(OUT_DIR / "itqs_bucket_performance.csv")
    fig = go.Figure()
    for sid in ["S3", "S4"]:
        sub = df[df.strategy_id == sid]
        fig.add_trace(go.Bar(x=sub["itqs_bucket"], y=sub["expectancy_r"], name=sid))
    fig.update_layout(title="ITQS Bucket Performance (Expectancy R)", template="plotly_white", barmode="group")
    return fig


def alpha_filter_chart() -> go.Figure:
    df = pd.read_csv(OUT_DIR / "alpha_filter_report.csv")
    df = df.sort_values("with_expectancy_r", ascending=True)
    colors = ["#2ca02c" if v == "ACCEPTED" else "#d62728" for v in df["verdict"]]
    fig = go.Figure(data=go.Bar(
        x=df["with_expectancy_r"], y=df["strategy_id"] + ": " + df["filter"],
        orientation="h", marker_color=colors,
    ))
    fig.update_layout(title="Alpha Filter Results (green=accepted, red=rejected)", template="plotly_white", height=900)
    return fig


def symbol_star_chart() -> go.Figure:
    df = pd.read_csv(OUT_DIR / "symbol_specialisation.csv")
    fig = go.Figure()
    for sid in ["S3", "S4"]:
        sub = df[df.strategy_id == sid].sort_values("composite", ascending=False)
        fig.add_trace(go.Bar(x=sub["symbol"], y=sub["composite"], name=sid))
    fig.update_layout(title="Symbol Specialisation Composite Score", template="plotly_white", barmode="group")
    return fig


def regime_heatmap() -> go.Figure:
    df = pd.read_csv(OUT_DIR / "market_regime_report.csv")
    figs = []
    fig = go.Figure()
    for sid in ["S3", "S4"]:
        sub = df[(df.strategy_id == sid) & (df.dimension == "session")]
        fig.add_trace(go.Bar(x=sub["condition"], y=sub["expectancy_weighted"], name=sid))
    fig.update_layout(title="Expectancy by Session", template="plotly_white", barmode="group")
    return fig


def entry_exit_chart() -> go.Figure:
    df = pd.read_csv(OUT_DIR / "entry_exit_refinement.csv")
    fig = go.Figure()
    for sweep in df["sweep"].unique():
        sub = df[df.sweep == sweep]
        if "value" not in sub.columns:
            continue
        fig.add_trace(go.Bar(x=[f"{sweep}={v}" for v in sub["value"]], y=sub["expectancy"], name=sweep, visible="legendonly" if "S4" in sweep else True))
    fig.update_layout(title="Entry/Exit Refinement Sweeps (Expectancy $, 3mo EURUSD)", template="plotly_white", xaxis_tickangle=-45, height=600, showlegend=True)
    return fig


def main() -> None:
    figs = {
        "feature_importance": feature_importance_chart(),
        "confluence": confluence_chart(),
        "itqs_buckets": itqs_bucket_chart(),
        "alpha_filters": alpha_filter_chart(),
        "symbol_specialisation": symbol_star_chart(),
        "regime_session": regime_heatmap(),
        "entry_exit_refinement": entry_exit_chart(),
    }
    parts = ["<html><head><title>Task 9 Edge Refinement Dashboard</title></head><body>",
             "<h1>Forex SMC Quant -- Institutional Edge Refinement Dashboard (S3/S4)</h1>"]
    for name, fig in figs.items():
        parts.append(f"<h2>{name.replace('_', ' ').title()}</h2>")
        parts.append(fig.to_html(full_html=False, include_plotlyjs="cdn"))
    parts.append("</body></html>")
    (OUT_DIR / "research_dashboard.html").write_text("\n".join(parts), encoding="utf-8")
    print(f"Wrote {OUT_DIR / 'research_dashboard.html'}")


if __name__ == "__main__":
    main()
