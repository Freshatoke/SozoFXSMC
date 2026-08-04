"""
Backtest visual validation.

Loads M1 data, generates Task 3 signals, runs the Task 4 backtest, and
renders an interactive candlestick chart with every trade drawn as a line
from entry to exit (green = winner, red = loser), plus entry/stop/target
markers, so a human can visually audit that the simulator is behaving
realistically -- this is NOT a trading UI.

Usage:
    python scripts/visualize_backtest.py --input data/raw/EURUSD_M1.csv \
        --symbol EURUSD --winners-only --strategies S3,S5 --min-confidence 80 \
        --out reports/backtest_validation.html
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd
import plotly.graph_objects as go

from src.data.loader import load_m1_csv
from src.strategies.context import MarketContext
from src.strategies.runner import run_strategies
from src.backtest.engine import run_backtest
from src.backtest.trade import TradeStatus
from config.settings import DEFAULT_RISK_CONFIG


def _filter_trades(trades, winners_only, losers_only, strategies, symbols, sessions, min_confidence, start, end):
    out = []
    for t in trades:
        if t.status != TradeStatus.CLOSED.value:
            continue
        if winners_only and t.realized_pnl <= 0:
            continue
        if losers_only and t.realized_pnl > 0:
            continue
        if strategies and t.strategy_id not in strategies:
            continue
        if symbols and t.symbol not in symbols:
            continue
        if sessions and t.session not in sessions:
            continue
        if t.confidence_score < min_confidence:
            continue
        if start and t.entry_timestamp < start:
            continue
        if end and t.entry_timestamp > end:
            continue
        out.append(t)
    return out


def build_figure(candles: pd.DataFrame, trades: list, symbol: str) -> go.Figure:
    fig = go.Figure()
    fig.add_trace(go.Candlestick(
        x=candles["timestamp"], open=candles["open"], high=candles["high"],
        low=candles["low"], close=candles["close"], name=symbol,
    ))

    for outcome, color in (("winning", "green"), ("losing", "red")):
        subset = [t for t in trades if (t.realized_pnl > 0) == (outcome == "winning")]
        if not subset:
            continue
        xs, ys, hover = [], [], []
        for t in subset:
            xs += [t.entry_timestamp, t.exit_timestamp, None]
            ys += [t.entry_price, t.exit_price, None]
            text = (
                f"{t.trade_id}<br>{t.strategy_id} {t.direction}<br>Entry: {t.entry_price:.5f} @ {t.entry_timestamp}"
                f"<br>Exit: {t.exit_price:.5f} @ {t.exit_timestamp} ({t.exit_reason})"
                f"<br>Duration: {t.duration_candles} candles<br>PnL: {t.realized_pnl:.2f}  R: {t.r_multiple}"
                f"<br>Confidence: {t.confidence_score}<br>Reason codes: {', '.join(t.reason_codes)}"
            )
            hover += [text, text, ""]
        fig.add_trace(go.Scatter(
            x=xs, y=ys, mode="lines+markers", line=dict(color=color, width=2),
            marker=dict(size=6), name=f"{outcome.capitalize()} trades", legendgroup=outcome,
            hovertext=hover, hoverinfo="text",
        ))

        for t in subset:
            fig.add_trace(go.Scatter(
                x=[t.entry_timestamp, t.entry_timestamp], y=[t.initial_stop_loss, t.entry_price],
                mode="lines", line=dict(color="orange", width=1, dash="dot"),
                showlegend=False, hoverinfo="skip",
            ))

    fig.update_layout(
        title=f"{symbol} — Backtest Validation ({len(trades)} trades shown)",
        xaxis_rangeslider_visible=False, template="plotly_white",
    )
    return fig


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--source-tz", default="UTC")
    ap.add_argument("--winners-only", action="store_true")
    ap.add_argument("--losers-only", action="store_true")
    ap.add_argument("--strategies", default=None, help="Comma-separated, e.g. S3,S5")
    ap.add_argument("--symbols", default=None)
    ap.add_argument("--sessions", default=None)
    ap.add_argument("--min-confidence", type=float, default=0.0)
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    m1, report = load_m1_csv(args.input, source_tz=args.source_tz)
    print(report.summary())

    context = MarketContext(symbol=args.symbol, m1=m1)
    signals = run_strategies(context)
    print(f"Generated {len(signals)} signals")

    trades = run_backtest(signals, m1, context=context)
    print(f"Simulated {len(trades)} trades")

    filtered = _filter_trades(
        trades, args.winners_only, args.losers_only,
        args.strategies.split(",") if args.strategies else None,
        args.symbols.split(",") if args.symbols else None,
        args.sessions.split(",") if args.sessions else None,
        args.min_confidence,
        pd.Timestamp(args.start, tz="UTC") if args.start else None,
        pd.Timestamp(args.end, tz="UTC") if args.end else None,
    )
    print(f"{len(filtered)} trades after filtering")

    fig = build_figure(m1, filtered, args.symbol)
    if args.out:
        fig.write_html(args.out)
        print(f"Saved chart to {args.out}")
    else:
        fig.show()


if __name__ == "__main__":
    main()
