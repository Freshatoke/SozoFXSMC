"""
Research Visualisations: one function per chart type named in the task
brief, each returning a standalone Plotly `Figure` so a researcher can
render exactly the chart they need (or assemble a subset into a
dashboard via `build_research_dashboard`).
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from src.backtest.metrics import build_equity_curve, trades_to_dataframe


def equity_curves_chart(trades_by_label: dict, starting_balance: float = 10_000.0) -> go.Figure:
    fig = go.Figure()
    for label, trades in trades_by_label.items():
        curve = build_equity_curve(trades, starting_balance)
        fig.add_trace(go.Scatter(x=curve["timestamp"], y=curve["balance"], mode="lines", name=label))
    fig.update_layout(title="Equity Curves", template="plotly_white")
    return fig


def drawdown_comparison_chart(trades_by_label: dict, starting_balance: float = 10_000.0) -> go.Figure:
    fig = go.Figure()
    for label, trades in trades_by_label.items():
        curve = build_equity_curve(trades, starting_balance)
        fig.add_trace(go.Scatter(x=curve["timestamp"], y=curve["drawdown_pct"] * 100, mode="lines", name=label))
    fig.update_layout(title="Drawdown Comparison (%)", template="plotly_white")
    return fig


def parameter_heatmap(sweep_results: pd.DataFrame, x_param: str, y_param: str, metric: str = "expectancy") -> go.Figure:
    pivot = sweep_results.pivot_table(index=y_param, columns=x_param, values=metric, aggfunc="mean")
    fig = go.Figure(data=go.Heatmap(z=pivot.values, x=pivot.columns, y=pivot.index, colorscale="RdYlGn"))
    fig.update_layout(title=f"Parameter Heatmap: {metric}", xaxis_title=x_param, yaxis_title=y_param, template="plotly_white")
    return fig


def sensitivity_curve_chart(response_curve: pd.DataFrame, param_column: str, metrics: tuple = ("expectancy", "profit_factor", "num_trades")) -> go.Figure:
    fig = make_subplots(rows=1, cols=len(metrics), subplot_titles=metrics)
    for i, metric in enumerate(metrics, start=1):
        fig.add_trace(go.Scatter(x=response_curve[param_column], y=response_curve[metric], mode="lines+markers", name=metric), row=1, col=i)
    fig.update_layout(title=f"Sensitivity Curve: {param_column}", template="plotly_white", showlegend=False)
    return fig


def confidence_distribution_chart(trades: list) -> go.Figure:
    df = trades_to_dataframe(trades)
    fig = go.Figure(data=go.Histogram(x=df["confidence_score"] if "confidence_score" in df.columns else []))
    fig.update_layout(title="Confidence Score Distribution", template="plotly_white")
    return fig


def strategy_comparison_chart(comparison: pd.DataFrame, metric: str = "expectancy") -> go.Figure:
    fig = go.Figure(data=go.Bar(x=comparison["strategy_id"], y=comparison[metric]))
    fig.update_layout(title=f"Strategy Comparison: {metric}", template="plotly_white")
    return fig


def portfolio_comparison_chart(portfolio_analysis: pd.DataFrame, metric: str = "expectancy") -> go.Figure:
    fig = go.Figure(data=go.Bar(x=portfolio_analysis["combination"], y=portfolio_analysis[metric]))
    fig.update_layout(title=f"Portfolio Comparison: {metric}", template="plotly_white", xaxis_tickangle=-45)
    return fig


def trade_distribution_chart(trades: list) -> go.Figure:
    df = trades_to_dataframe(trades)
    fig = go.Figure(data=go.Histogram(x=df["realized_pnl"] if "realized_pnl" in df.columns else []))
    fig.update_layout(title="Trade PnL Distribution", template="plotly_white")
    return fig


def session_performance_chart(session_analysis: pd.DataFrame) -> go.Figure:
    fig = go.Figure(data=go.Bar(x=session_analysis["session"], y=session_analysis["expectancy"]))
    fig.update_layout(title="Session Performance (Expectancy)", template="plotly_white")
    return fig


def symbol_performance_chart(symbol_analysis: pd.DataFrame) -> go.Figure:
    fig = go.Figure(data=go.Bar(x=symbol_analysis["symbol"], y=symbol_analysis["expectancy"]))
    fig.update_layout(title="Symbol Performance (Expectancy)", template="plotly_white")
    return fig


def correlation_matrix_chart(correlation: pd.DataFrame) -> go.Figure:
    fig = go.Figure(data=go.Heatmap(z=correlation.values, x=correlation.columns, y=correlation.index, colorscale="RdBu", zmid=0))
    fig.update_layout(title="Strategy Correlation Matrix", template="plotly_white")
    return fig


def monthly_returns_chart(trades: list, starting_balance: float = 10_000.0) -> go.Figure:
    curve = build_equity_curve(trades, starting_balance).dropna(subset=["timestamp"])
    if curve.empty:
        return go.Figure()
    curve = curve.copy()
    curve["month"] = curve["timestamp"].dt.tz_localize(None).dt.to_period("M")
    monthly = curve.groupby("month")["balance"].agg(["first", "last"])
    monthly["return_pct"] = (monthly["last"] - monthly["first"]) / monthly["first"] * 100
    fig = go.Figure(data=go.Bar(x=monthly.index.astype(str), y=monthly["return_pct"]))
    fig.update_layout(title="Monthly Returns (%)", template="plotly_white")
    return fig


def expectancy_distribution_chart(group_analysis: pd.DataFrame, group_column: str) -> go.Figure:
    fig = go.Figure(data=go.Bar(x=group_analysis[group_column], y=group_analysis["expectancy"]))
    fig.update_layout(title=f"Expectancy Distribution by {group_column}", template="plotly_white")
    return fig


def build_research_dashboard(
    trades: list, comparison: pd.DataFrame, portfolio_analysis: pd.DataFrame,
    session_analysis: pd.DataFrame, symbol_analysis: pd.DataFrame, correlation: pd.DataFrame,
    starting_balance: float = 10_000.0,
) -> go.Figure:
    fig = make_subplots(
        rows=3, cols=2,
        subplot_titles=("Equity Curve", "Trade PnL Distribution", "Strategy Comparison", "Portfolio Comparison", "Session Performance", "Symbol Performance"),
    )
    curve = build_equity_curve(trades, starting_balance)
    fig.add_trace(go.Scatter(x=curve["timestamp"], y=curve["balance"], mode="lines", name="Equity"), row=1, col=1)

    df = trades_to_dataframe(trades)
    if "realized_pnl" in df.columns:
        fig.add_trace(go.Histogram(x=df["realized_pnl"], name="PnL"), row=1, col=2)

    if not comparison.empty:
        fig.add_trace(go.Bar(x=comparison["strategy_id"], y=comparison["expectancy"], name="Strategy"), row=2, col=1)
    if not portfolio_analysis.empty:
        fig.add_trace(go.Bar(x=portfolio_analysis["combination"], y=portfolio_analysis["expectancy"], name="Portfolio"), row=2, col=2)
    if not session_analysis.empty:
        fig.add_trace(go.Bar(x=session_analysis["session"], y=session_analysis["expectancy"], name="Session"), row=3, col=1)
    if not symbol_analysis.empty:
        fig.add_trace(go.Bar(x=symbol_analysis["symbol"], y=symbol_analysis["expectancy"], name="Symbol"), row=3, col=2)

    fig.update_layout(height=1200, title="Research Lab Dashboard", template="plotly_white", showlegend=False)
    return fig
