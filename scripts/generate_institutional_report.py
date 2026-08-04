"""
Task 8 — builds every required export, visualization, and the final
institutional research report from the cached results produced by
`scripts/run_institutional_research.py` (reports/institutional_research/_cache/).

Kept as a SEPARATE script from the research run itself so report/chart
iteration never requires re-running the (expensive) backtests.
"""

from __future__ import annotations

import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.backtest.metrics import build_equity_curve, max_drawdown
from src.backtest.portfolio import combine_trades
from src.research.institutional_edge import IES_WEIGHTS
from src.features.storage import save_feature_dataset

CACHE_DIR = ROOT / "reports" / "institutional_research" / "_cache"
OUT_DIR = ROOT / "reports" / "institutional_research"
STRATEGY_IDS = ["S1", "S2", "S3", "S4", "S5"]
STARTING_BALANCE = 10_000.0


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# Load cache
# ---------------------------------------------------------------------------

def load_all():
    per_symbol = {}
    for path in sorted(CACHE_DIR.glob("*_tier1.pkl")):
        symbol = path.name.replace("_tier1.pkl", "")
        with open(path, "rb") as f:
            per_symbol[symbol] = pickle.load(f)
    tier2 = tier3 = None
    if (CACHE_DIR / "EURUSD_tier2.pkl").exists():
        with open(CACHE_DIR / "EURUSD_tier2.pkl", "rb") as f:
            tier2 = pickle.load(f)
    if (CACHE_DIR / "EURUSD_tier3.pkl").exists():
        with open(CACHE_DIR / "EURUSD_tier3.pkl", "rb") as f:
            tier3 = pickle.load(f)
    with open(CACHE_DIR / "aggregate.pkl", "rb") as f:
        agg = pickle.load(f)
    return per_symbol, tier2, tier3, agg


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

def write_csv_exports(agg: dict, per_symbol: dict) -> None:
    agg["ies_table"].to_csv(OUT_DIR / "institutional_edge_rankings.csv", index=False)

    portfolio = agg["portfolio_combined"].copy()
    portfolio["recommended_for_live"] = (
        (portfolio["expectancy"] > 0) & (portfolio["diversification_benefit"] >= 0) & (portfolio["profit_factor"] > 1.0)
    )
    portfolio.to_csv(OUT_DIR / "portfolio_recommendations.csv", index=False)
    log("Wrote institutional_edge_rankings.csv, portfolio_recommendations.csv")


def write_parquet_exports(per_symbol: dict) -> None:
    failure_frames = [r["failure"] for r in per_symbol.values() if not r["failure"].empty]
    failure_all = pd.concat(failure_frames, ignore_index=True) if failure_frames else pd.DataFrame()
    save_feature_dataset(failure_all, OUT_DIR / "failure_analysis.parquet", index=False)

    regime_frames = [r["regime"] for r in per_symbol.values() if not r["regime"].empty]
    regime_all = pd.concat(regime_frames, ignore_index=True) if regime_frames else pd.DataFrame()
    save_feature_dataset(regime_all, OUT_DIR / "market_regime_analysis.parquet", index=False)

    confidence_frames = [r["confidence_buckets"] for r in per_symbol.values() if not r["confidence_buckets"].empty]
    confidence_all = pd.concat(confidence_frames, ignore_index=True) if confidence_frames else pd.DataFrame()
    save_feature_dataset(confidence_all, OUT_DIR / "confidence_validation.parquet", index=False)
    log("Wrote failure_analysis.parquet, market_regime_analysis.parquet, confidence_validation.parquet")
    return failure_all, regime_all, confidence_all


# ---------------------------------------------------------------------------
# Visualizations
# ---------------------------------------------------------------------------

def equity_curves_chart(trades_by_strategy: dict) -> go.Figure:
    fig = go.Figure()
    for sid, trades in trades_by_strategy.items():
        curve = build_equity_curve(trades, STARTING_BALANCE)
        fig.add_trace(go.Scatter(x=curve["timestamp"], y=curve["balance"], mode="lines", name=sid))
    fig.update_layout(title="Equity Curves by Strategy (EURUSD, full history)", template="plotly_white")
    return fig


def drawdown_curves_chart(trades_by_strategy: dict) -> go.Figure:
    fig = go.Figure()
    for sid, trades in trades_by_strategy.items():
        curve = build_equity_curve(trades, STARTING_BALANCE)
        fig.add_trace(go.Scatter(x=curve["timestamp"], y=curve["drawdown_pct"] * 100, mode="lines", name=sid))
    fig.update_layout(title="Drawdown Curves by Strategy (%)", template="plotly_white")
    return fig


def rolling_expectancy_chart(window_detail: dict) -> go.Figure:
    fig = make_subplots(rows=1, cols=2, subplot_titles=("Rolling 6-Month Expectancy", "Rolling 12-Month Expectancy"))
    for sid in STRATEGY_IDS:
        df6 = window_detail.get((sid, "6mo"))
        if df6 is not None and not df6.empty:
            fig.add_trace(go.Scatter(x=df6["test_start"], y=df6["expectancy"], mode="lines+markers", name=f"{sid} (6mo)"), row=1, col=1)
        df12 = window_detail.get((sid, "12mo"))
        if df12 is not None and not df12.empty:
            fig.add_trace(go.Scatter(x=df12["test_start"], y=df12["expectancy"], mode="lines+markers", name=f"{sid} (12mo)"), row=1, col=2)
    fig.add_hline(y=0, line_dash="dash", line_color="gray")
    fig.update_layout(title="Rolling-Window Expectancy Stability (EURUSD)", template="plotly_white", height=450)
    return fig


def monthly_heatmap_chart(month_by_month: pd.DataFrame) -> go.Figure:
    if month_by_month.empty:
        return go.Figure()
    pivot = month_by_month.pivot_table(index="strategy_id", columns="year_month", values="expectancy", aggfunc="mean")
    fig = go.Figure(data=go.Heatmap(z=pivot.values, x=pivot.columns.astype(str), y=pivot.index, colorscale="RdYlGn", zmid=0))
    fig.update_layout(title="Monthly Expectancy Heatmap (EURUSD)", template="plotly_white", height=350)
    return fig


def strategy_comparison_chart(ies_table: pd.DataFrame) -> go.Figure:
    fig = go.Figure()
    for metric, label in [("raw_r_multiple_mean", "Avg R"), ("raw_profit_factor", "Profit Factor")]:
        fig.add_trace(go.Bar(x=ies_table["strategy_id"], y=ies_table[metric], name=label))
    fig.update_layout(title="Strategy Comparison (combined across symbols)", template="plotly_white", barmode="group")
    return fig


def portfolio_comparison_chart(portfolio: pd.DataFrame, top_n: int = 15) -> go.Figure:
    top = portfolio.sort_values("expectancy", ascending=False).head(top_n)
    fig = go.Figure(data=go.Bar(x=top["combination"], y=top["expectancy"]))
    fig.update_layout(title=f"Top {top_n} Portfolio Combinations by Expectancy", template="plotly_white", xaxis_tickangle=-45)
    return fig


def correlation_matrix_chart(correlation: pd.DataFrame) -> go.Figure:
    fig = go.Figure(data=go.Heatmap(z=correlation.values, x=correlation.columns, y=correlation.index, colorscale="RdBu", zmid=0))
    fig.update_layout(title="Strategy Correlation Matrix (combined across symbols)", template="plotly_white")
    return fig


def confidence_distribution_chart(confidence_all: pd.DataFrame) -> go.Figure:
    if confidence_all.empty:
        return go.Figure()
    fig = go.Figure()
    for symbol, grp in confidence_all.groupby("symbol"):
        fig.add_trace(go.Bar(x=grp["confidence_range"], y=grp["expectancy"], name=symbol))
    fig.update_layout(title="Expectancy by Confidence Bucket (per symbol)", template="plotly_white", barmode="group")
    return fig


def failure_distribution_chart(failure_all: pd.DataFrame) -> go.Figure:
    if failure_all.empty:
        return go.Figure()
    agg = failure_all.groupby("failure_category")["count"].sum().sort_values(ascending=False)
    fig = go.Figure(data=go.Bar(x=agg.index, y=agg.values))
    fig.update_layout(title="Failure Category Frequency (all symbols/strategies)", template="plotly_white")
    return fig


def regime_performance_chart(regime_all: pd.DataFrame, dimension: str = "trend_state") -> go.Figure:
    if regime_all.empty:
        return go.Figure()
    subset = regime_all[regime_all["dimension"] == dimension]
    pivot = subset.pivot_table(index="strategy_id", columns="condition", values="expectancy", aggfunc="mean")
    fig = go.Figure(data=go.Heatmap(z=pivot.values, x=pivot.columns, y=pivot.index, colorscale="RdYlGn", zmid=0))
    fig.update_layout(title=f"Expectancy by {dimension} (all symbols)", template="plotly_white")
    return fig


def ies_ranking_chart(ies_table: pd.DataFrame) -> go.Figure:
    fig = go.Figure(data=go.Bar(x=ies_table["strategy_id"], y=ies_table["institutional_edge_score"]))
    fig.update_layout(title="Institutional Edge Score Ranking", template="plotly_white")
    return fig


def build_all_visualizations(per_symbol: dict, tier2: dict, tier3: dict, agg: dict,
                              failure_all: pd.DataFrame, regime_all: pd.DataFrame, confidence_all: pd.DataFrame) -> dict:
    figs = {}
    eur_trades = per_symbol["EURUSD"]["trades_by_strategy"] if "EURUSD" in per_symbol else {}
    if eur_trades:
        figs["equity_curves"] = equity_curves_chart(eur_trades)
        figs["drawdown_curves"] = drawdown_curves_chart(eur_trades)
    if tier2:
        figs["rolling_expectancy"] = rolling_expectancy_chart(tier2["window_detail"])
        figs["monthly_heatmap"] = monthly_heatmap_chart(tier2["month_by_month"])
    figs["strategy_comparison"] = strategy_comparison_chart(agg["ies_table"])
    figs["portfolio_comparison"] = portfolio_comparison_chart(agg["portfolio_combined"])
    figs["correlation_matrix"] = correlation_matrix_chart(agg["correlation_combined"])
    figs["confidence_distribution"] = confidence_distribution_chart(confidence_all)
    figs["failure_distribution"] = failure_distribution_chart(failure_all)
    figs["regime_trend"] = regime_performance_chart(regime_all, "trend_state")
    figs["regime_volatility"] = regime_performance_chart(regime_all, "volatility_state")
    figs["regime_session"] = regime_performance_chart(regime_all, "session")
    figs["ies_ranking"] = ies_ranking_chart(agg["ies_table"])
    return figs


def write_dashboard_html(figs: dict) -> None:
    parts = ["<html><head><title>Institutional Research Dashboard</title></head><body>",
             "<h1>Forex SMC Quant — Institutional Research Dashboard</h1>"]
    for name, fig in figs.items():
        if fig is None or not fig.data:
            continue
        parts.append(f"<h2>{name.replace('_', ' ').title()}</h2>")
        parts.append(fig.to_html(full_html=False, include_plotlyjs="cdn"))
    parts.append("</body></html>")
    (OUT_DIR / "research_dashboard.html").write_text("\n".join(parts), encoding="utf-8")
    log("Wrote research_dashboard.html")


if __name__ == "__main__":
    log("Loading cached research results...")
    per_symbol, tier2, tier3, agg = load_all()
    log(f"Symbols loaded: {list(per_symbol.keys())}, missing: {agg['missing_symbols']}")

    write_csv_exports(agg, per_symbol)
    failure_all, regime_all, confidence_all = write_parquet_exports(per_symbol)
    figs = build_all_visualizations(per_symbol, tier2, tier3, agg, failure_all, regime_all, confidence_all)
    write_dashboard_html(figs)
    log("Done (exports + dashboard). Report/PDF generation is a separate step.")
