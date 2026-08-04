"""
Visual validation script.

Loads M1 data, resamples to a target timeframe, runs the Market Structure
Engine (Task 1) and the SMC Feature Engine (Task 2), and renders an
interactive Plotly candlestick chart annotated with everything a human
needs to audit correctness:

    candlesticks, confirmed swing highs/lows, BOS/CHoCH markers,
    Order Blocks, Fair Value Gaps, liquidity levels, PDH/PDL, PWH/PWL,
    Asian/London/New York session highs-lows, weekend gaps, and (with
    --signals) Strategy Engine (Task 3) signal markers.

Every category is its own Plotly trace, so it can be toggled on/off by
clicking its entry in the legend -- this is NOT a polished trading UI, it
exists purely so a human can look at a chart and confirm the algorithm is
right.

Usage:
    python scripts/validate_structure.py --input data/raw/EURUSD_M1.csv \
        --symbol EURUSD --timeframe 15min --start 2023-01-01 --end 2023-02-01

    # with Strategy Engine signal markers, filtered:
    python scripts/validate_structure.py --input data/raw/EURUSD_M1.csv \
        --symbol EURUSD --signals --strategies S3,S5 --min-confidence 80 \
        --direction bullish --out reports/signals.html
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd
import plotly.graph_objects as go

from config.settings import (
    SwingConfig, StructureConfig, DisplacementConfig, OrderBlockConfig,
    FVGConfig, LiquidityConfig,
)
from src.data.loader import load_m1_csv
from src.data.resample import resample_ohlc
from src.structure.swings import detect_swings
from src.structure.market_structure import detect_structure_events
from src.features.order_blocks import detect_order_blocks
from src.features.fvg import detect_fvgs
from src.features.liquidity import detect_liquidity_levels
from src.features.reference_levels import compute_reference_levels, compute_weekend_gaps
from src.features.sessions import compute_sessions
from src.strategies.context import MarketContext
from src.strategies.runner import run_strategies

STRATEGY_COLORS = {
    "S1": "#8e44ad", "S2": "#2980b9", "S3": "#c0392b", "S4": "#16a085", "S5": "#d35400",
}


def _rect_trace(boxes, color, name, opacity=0.25):
    """boxes: list of (x0, x1, y0, y1). Returns one togglable filled trace
    containing all rectangles, separated by None gaps."""
    xs, ys = [], []
    for x0, x1, y0, y1 in boxes:
        xs += [x0, x1, x1, x0, x0, None]
        ys += [y0, y0, y1, y1, y0, None]
    return go.Scatter(
        x=xs, y=ys, mode="lines", fill="toself",
        line=dict(color=color, width=1), fillcolor=color, opacity=opacity,
        name=name, legendgroup=name,
    )


def _signal_traces(signals: list, min_confidence: float = 0.0, strategies: list | None = None, direction: str | None = None) -> list:
    """One togglable trace per strategy_id. Hover text shows the full
    reason-code list and confidence score so a human can see exactly why
    each signal fired. `strategies`/`direction`/`min_confidence` implement
    the required filter-by-strategy/direction/confidence controls."""
    filtered = [
        s for s in signals
        if s.confidence_score >= min_confidence
        and (strategies is None or s.strategy_id in strategies)
        and (direction is None or s.direction == direction)
    ]
    traces = []
    for strategy_id in sorted({s.strategy_id for s in filtered}):
        subset = [s for s in filtered if s.strategy_id == strategy_id]
        color = STRATEGY_COLORS.get(strategy_id, "gray")
        for dirn, symbol_, pos in (("bullish", "triangle-up", "bottom center"), ("bearish", "triangle-down", "top center")):
            dir_subset = [s for s in subset if s.direction == dirn]
            if not dir_subset:
                continue
            traces.append(go.Scatter(
                x=[s.timestamp for s in dir_subset],
                y=[s.entry_zone[1] if dirn == "bullish" else s.entry_zone[0] for s in dir_subset],
                mode="markers", marker=dict(symbol=symbol_, size=14, color=color, line=dict(width=1, color="black")),
                name=f"{strategy_id} {dirn}", legendgroup=f"signal_{strategy_id}",
                hovertext=[
                    f"{s.signal_id}<br>Confidence: {s.confidence_score}<br>Reason codes: {', '.join(s.reason_codes)}"
                    f"<br>Entry zone: {s.entry_zone}<br>Stop: {s.stop_loss_reference}<br>Target: {s.target_reference}"
                    for s in dir_subset
                ],
                hoverinfo="text",
            ))
    return traces


def build_figure(
    candles: pd.DataFrame, swings: pd.DataFrame, events: pd.DataFrame,
    order_blocks: pd.DataFrame, fvgs: pd.DataFrame, liquidity: pd.DataFrame,
    reference_levels: pd.DataFrame, weekend_gaps: pd.DataFrame, sessions: pd.DataFrame,
    symbol: str, timeframe: str, signals: list | None = None,
    min_confidence: float = 0.0, strategies: list | None = None, direction: str | None = None,
) -> go.Figure:
    fig = go.Figure()
    chart_end = candles["timestamp"].iloc[-1]

    fig.add_trace(go.Candlestick(
        x=candles["timestamp"], open=candles["open"], high=candles["high"],
        low=candles["low"], close=candles["close"], name=f"{symbol} {timeframe}",
    ))

    # --- swings ---
    swing_highs = swings[swings["swing_type"] == "high"]
    swing_lows = swings[swings["swing_type"] == "low"]
    fig.add_trace(go.Scatter(
        x=swing_highs["swing_timestamp"], y=swing_highs["price"], mode="markers",
        marker=dict(symbol="triangle-down", size=10, color="orange"), name="Confirmed Swing High",
    ))
    fig.add_trace(go.Scatter(
        x=swing_lows["swing_timestamp"], y=swing_lows["price"], mode="markers",
        marker=dict(symbol="triangle-up", size=10, color="blue"), name="Confirmed Swing Low",
    ))

    # --- BOS / CHoCH ---
    if not events.empty:
        for etype, direction, symbol_, color, pos in [
            ("BOS", "bullish", "star", "green", "top center"),
            ("BOS", "bearish", "star", "red", "bottom center"),
            ("CHoCH", "bullish", "diamond", "darkgreen", "top center"),
            ("CHoCH", "bearish", "diamond", "darkred", "bottom center"),
        ]:
            subset = events[(events.event_type == etype) & (events.direction == direction)]
            label = f"{'Bullish' if direction == 'bullish' else 'Bearish'} {etype}"
            fig.add_trace(go.Scatter(
                x=subset["break_candle_timestamp"], y=subset["break_candle_close"], mode="markers+text",
                marker=dict(symbol=symbol_, size=12, color=color),
                text=[("+" if direction == "bullish" else "-")] * len(subset),
                textposition=pos, name=label,
            ))

    # --- Order Blocks ---
    if not order_blocks.empty:
        for direction, color in [("bullish", "rgba(0,150,0,0.9)"), ("bearish", "rgba(200,0,0,0.9)")]:
            subset = order_blocks[order_blocks.direction == direction]
            boxes = [
                (row["creation_timestamp"], chart_end, row["low"], row["high"])
                for _, row in subset.iterrows()
            ]
            if boxes:
                fig.add_trace(_rect_trace(boxes, color, f"{direction.capitalize()} Order Block"))

    # --- Fair Value Gaps ---
    if not fvgs.empty:
        for direction, color in [("bullish", "rgba(0,100,255,0.9)"), ("bearish", "rgba(255,140,0,0.9)")]:
            subset = fvgs[fvgs.direction == direction]
            boxes = [
                (row["creation_timestamp"], chart_end, row["bottom"], row["top"])
                for _, row in subset.iterrows()
            ]
            if boxes:
                fig.add_trace(_rect_trace(boxes, color, f"{direction.capitalize()} FVG"))

    # --- Liquidity levels ---
    if not liquidity.empty:
        for side, color in [("buy_side", "purple"), ("sell_side", "brown")]:
            subset = liquidity[liquidity.side == side]
            for _, row in subset.iterrows():
                end_ts = row["swept_timestamp"] if pd.notna(row["swept_timestamp"]) else chart_end
                fig.add_trace(go.Scatter(
                    x=[row["creation_timestamp"], end_ts], y=[row["price"], row["price"]],
                    mode="lines", line=dict(color=color, width=1, dash="dash"),
                    name=f"{'Buy-side' if side == 'buy_side' else 'Sell-side'} Liquidity ({row['type']})",
                    legendgroup=f"liquidity_{side}", showlegend=bool(subset.index[0] == row.name),
                    opacity=0.7,
                ))

    # --- Reference levels: PDH/PDL/PWH/PWL ---
    if not reference_levels.empty:
        for level_type, color in [("PDH", "teal"), ("PDL", "teal"), ("PWH", "black"), ("PWL", "black")]:
            subset = reference_levels[reference_levels.level_type == level_type]
            for i, (_, row) in enumerate(subset.iterrows()):
                fig.add_trace(go.Scatter(
                    x=[row["available_from"], chart_end], y=[row["value"], row["value"]],
                    mode="lines", line=dict(color=color, width=1, dash="dot"),
                    name=level_type, legendgroup=level_type, showlegend=(i == 0), opacity=0.6,
                ))

    # --- Session highs/lows (Asian, London, New York) ---
    if not sessions.empty:
        for session_name, color in [("tokyo", "gold"), ("london", "skyblue"), ("new_york", "salmon")]:
            subset = sessions[sessions.session_name == session_name]
            for i, (_, row) in enumerate(subset.iterrows()):
                fig.add_trace(go.Scatter(
                    x=[row["start_utc"], row["end_utc"]], y=[row["high"], row["high"]],
                    mode="lines", line=dict(color=color, width=2),
                    name=f"{session_name.capitalize()} Session High/Low",
                    legendgroup=f"session_{session_name}", showlegend=(i == 0), opacity=0.8,
                ))
                fig.add_trace(go.Scatter(
                    x=[row["start_utc"], row["end_utc"]], y=[row["low"], row["low"]],
                    mode="lines", line=dict(color=color, width=2),
                    legendgroup=f"session_{session_name}", showlegend=False, opacity=0.8,
                ))

    # --- Weekend gaps ---
    if not weekend_gaps.empty:
        for i, (_, row) in enumerate(weekend_gaps.iterrows()):
            fig.add_trace(go.Scatter(
                x=[row["friday_close_timestamp"], row["reopen_timestamp"]],
                y=[row["friday_close"], row["reopen_open"]],
                mode="lines+markers", line=dict(color="magenta", width=2, dash="dashdot"),
                name="Weekend Gap", legendgroup="weekend_gap", showlegend=(i == 0),
            ))

    # --- Strategy Engine signals (Task 3) ---
    if signals:
        for trace in _signal_traces(signals, min_confidence=min_confidence, strategies=strategies, direction=direction):
            fig.add_trace(trace)

    fig.update_layout(
        title=f"{symbol} {timeframe} — Market Structure + SMC Feature Validation",
        xaxis_rangeslider_visible=False,
        template="plotly_white",
        legend=dict(groupclick="togglegroup"),
    )
    return fig


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="Path to raw M1 CSV/Parquet file")
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--timeframe", default="15min", help="Target timeframe e.g. 5min, 15min")
    ap.add_argument("--source-tz", default="UTC")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--left", type=int, default=2)
    ap.add_argument("--right", type=int, default=2)
    ap.add_argument("--out", default=None, help="Optional HTML output path instead of opening a browser")
    ap.add_argument("--signals", action="store_true", help="Compute and overlay Strategy Engine (Task 3) signals")
    ap.add_argument("--strategies", default=None, help="Comma-separated strategy filter, e.g. S3,S5")
    ap.add_argument("--min-confidence", type=float, default=0.0)
    ap.add_argument("--direction", default=None, choices=["bullish", "bearish"])
    args = ap.parse_args()

    m1, report = load_m1_csv(args.input, source_tz=args.source_tz)
    print(report.summary())

    candles = resample_ohlc(m1, args.timeframe) if args.timeframe != "1min" else m1

    if args.start:
        candles = candles[candles["timestamp"] >= pd.Timestamp(args.start, tz="UTC")]
    if args.end:
        candles = candles[candles["timestamp"] <= pd.Timestamp(args.end, tz="UTC")]
    candles = candles.reset_index(drop=True)

    swing_cfg = SwingConfig(left=args.left, right=args.right)
    structure_cfg = StructureConfig(swing=swing_cfg)

    swings = detect_swings(candles, config=swing_cfg, timeframe_label=args.timeframe)
    events = detect_structure_events(candles, swings, symbol=args.symbol, timeframe=args.timeframe, config=structure_cfg)

    order_blocks, ob_skipped = detect_order_blocks(
        candles, args.symbol, args.timeframe,
        config=OrderBlockConfig(displacement=DisplacementConfig()), structure_events=events,
    )
    fvgs = detect_fvgs(candles, args.symbol, args.timeframe, config=FVGConfig())
    liquidity = detect_liquidity_levels(
        candles, args.symbol, args.timeframe,
        config=LiquidityConfig(swing_left=args.left, swing_right=args.right),
    )
    reference_levels = compute_reference_levels(m1)
    weekend_gaps = compute_weekend_gaps(m1)
    sessions = compute_sessions(m1)

    print(
        f"Detected {len(swings)} confirmed swings, {len(events)} structure events, "
        f"{len(order_blocks)} order blocks ({len(ob_skipped)} displacement events skipped), "
        f"{len(fvgs)} FVGs, {len(liquidity)} liquidity levels, "
        f"{len(reference_levels)} reference levels, {len(weekend_gaps)} weekend gaps."
    )

    signals = None
    if args.signals:
        context = MarketContext(symbol=args.symbol, m1=m1)
        signals = run_strategies(context)
        print(f"Generated {len(signals)} strategy signals across {len({s.strategy_id for s in signals})} strategies.")

    strategies_filter = args.strategies.split(",") if args.strategies else None

    fig = build_figure(
        candles, swings, events, order_blocks, fvgs, liquidity,
        reference_levels, weekend_gaps, sessions, args.symbol, args.timeframe,
        signals=signals, min_confidence=args.min_confidence,
        strategies=strategies_filter, direction=args.direction,
    )

    if args.out:
        fig.write_html(args.out)
        print(f"Saved chart to {args.out}")
    else:
        fig.show()


if __name__ == "__main__":
    main()
