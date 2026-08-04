"""
Reporting: writes the four required datasets
(backtest_results.parquet, trade_history.parquet, equity_curve.parquet,
performance_summary.json) and builds the visual report dashboard
(equity curve, drawdown, monthly returns, trade/win-loss distribution,
MAE vs MFE, R-multiple histogram, strategy comparison, session
performance) as one self-contained HTML file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from src.backtest.trade import TradeStatus
from src.backtest.metrics import trades_to_dataframe, build_equity_curve, compute_performance_metrics, _closed

SLIM_COLUMNS = [
    "trade_id", "signal_id", "strategy_id", "symbol", "timeframe", "direction", "status",
    "signal_timestamp", "entry_method", "stop_method", "take_profit_method",
    "entry_price", "entry_timestamp", "initial_stop_loss", "position_size", "risk_amount",
    "exit_price", "exit_timestamp", "exit_reason", "realized_pnl", "commission_paid",
    "mae", "mfe", "duration_candles", "r_multiple", "confidence_score", "session", "rejection_reason",
]


def _json_default(obj):
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return str(obj)


def save_trade_history(trades: list, path: str | Path) -> None:
    """Full per-trade record, including nested management events / partial
    exits / reason codes / confluence snapshot (JSON-encoded)."""
    df = trades_to_dataframe(trades)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    out = df.copy()
    for col in ("reason_codes", "confluence_snapshot", "take_profit_levels", "partial_exits", "management_events", "metadata"):
        if col in out.columns:
            out[col] = out[col].apply(lambda v: json.dumps(v, default=_json_default))
    out.to_parquet(p, index=False)


def save_backtest_results(trades: list, path: str | Path) -> None:
    """Slim, flat, numeric-friendly view -- one row per trade, only
    scalar fields -- suited for quick aggregate analysis without needing
    to parse any JSON-encoded column."""
    df = trades_to_dataframe(trades)
    cols = [c for c in SLIM_COLUMNS if c in df.columns]
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    df[cols].to_parquet(p, index=False)


def save_equity_curve(trades: list, starting_balance: float, path: str | Path) -> pd.DataFrame:
    curve = build_equity_curve(trades, starting_balance)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    curve.to_parquet(p, index=False)
    return curve


def save_performance_summary(metrics: dict, path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(metrics, indent=2, default=_json_default))


def generate_all_reports(trades: list, starting_balance: float, out_dir: str | Path) -> dict:
    out_dir = Path(out_dir)
    metrics = compute_performance_metrics(trades, starting_balance)
    equity_curve = save_equity_curve(trades, starting_balance, out_dir / "equity_curve.parquet")
    save_trade_history(trades, out_dir / "trade_history.parquet")
    save_backtest_results(trades, out_dir / "backtest_results.parquet")
    save_performance_summary(metrics, out_dir / "performance_summary.json")
    return {"metrics": metrics, "equity_curve": equity_curve}


def _monthly_returns(equity_curve: pd.DataFrame) -> pd.DataFrame:
    curve = equity_curve.dropna(subset=["timestamp"]).copy()
    if curve.empty:
        return pd.DataFrame(columns=["month", "return_pct"])
    curve["month"] = curve["timestamp"].dt.tz_localize(None).dt.to_period("M")
    monthly = curve.groupby("month")["balance"].agg(["first", "last"])
    monthly["return_pct"] = (monthly["last"] - monthly["first"]) / monthly["first"]
    return monthly.reset_index()


def build_dashboard(trades: list, metrics: dict, equity_curve: pd.DataFrame, comparison: pd.DataFrame | None = None) -> go.Figure:
    closed = _closed(trades)
    fig = make_subplots(
        rows=4, cols=2,
        subplot_titles=(
            "Equity Curve", "Drawdown Curve", "Monthly Returns", "Trade PnL Distribution",
            "Win/Loss Count", "MAE vs MFE", "R-Multiple Histogram", "Expectancy by Strategy/Session",
        ),
    )

    if not equity_curve.empty:
        fig.add_trace(go.Scatter(x=equity_curve["timestamp"], y=equity_curve["balance"], mode="lines", name="Equity"), row=1, col=1)
        fig.add_trace(go.Scatter(x=equity_curve["timestamp"], y=equity_curve["drawdown_pct"] * 100, mode="lines", fill="tozeroy", name="Drawdown %"), row=1, col=2)

    monthly = _monthly_returns(equity_curve)
    if not monthly.empty:
        fig.add_trace(go.Bar(x=monthly["month"].astype(str), y=monthly["return_pct"] * 100, name="Monthly Return %"), row=2, col=1)

    pnls = [t.realized_pnl for t in closed]
    if pnls:
        fig.add_trace(go.Histogram(x=pnls, name="Trade PnL"), row=2, col=2)

    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p <= 0)
    fig.add_trace(go.Bar(x=["Wins", "Losses"], y=[wins, losses], name="Win/Loss"), row=3, col=1)

    if closed:
        fig.add_trace(go.Scatter(
            x=[t.mae for t in closed], y=[t.mfe for t in closed], mode="markers",
            marker=dict(color=["green" if t.realized_pnl > 0 else "red" for t in closed]), name="MAE vs MFE",
        ), row=3, col=2)

    r_values = metrics.get("r_multiple_distribution", {}).get("values", [])
    if r_values:
        fig.add_trace(go.Histogram(x=r_values, name="R-Multiple"), row=4, col=1)

    if comparison is not None and not comparison.empty:
        fig.add_trace(go.Bar(x=comparison["strategy_id"], y=comparison["expectancy"], name="Expectancy by Strategy"), row=4, col=2)
    else:
        by_session = metrics.get("expectancy_by_session", {})
        if by_session:
            fig.add_trace(go.Bar(x=list(by_session.keys()), y=[v["expectancy"] for v in by_session.values()], name="Expectancy by Session"), row=4, col=2)

    fig.update_layout(height=1400, title="Backtest Performance Dashboard", template="plotly_white", showlegend=False)
    return fig


def save_dashboard_html(trades: list, starting_balance: float, path: str | Path, comparison: pd.DataFrame | None = None) -> None:
    metrics = compute_performance_metrics(trades, starting_balance)
    equity_curve = build_equity_curve(trades, starting_balance)
    fig = build_dashboard(trades, metrics, equity_curve, comparison)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(str(p))
