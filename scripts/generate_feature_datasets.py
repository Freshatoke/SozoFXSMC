"""
Runs the full SMC Feature Engine over a historical M1 file and writes every
dataset (order_blocks, fvgs, liquidity, sessions, reference_levels,
engulfing, confluence) to data/processed/ as Parquet.

Usage:
    python scripts/generate_feature_datasets.py --input data/raw/EURUSD_M1.csv \
        --symbol EURUSD --timeframe 15min
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd

from config.settings import SwingConfig, StructureConfig, OrderBlockConfig, FVGConfig, LiquidityConfig
from src.data.loader import load_m1_csv
from src.data.resample import resample_ohlc
from src.structure.swings import detect_swings
from src.structure.market_structure import detect_structure_events
from src.features.order_blocks import detect_order_blocks
from src.features.fvg import detect_fvgs
from src.features.liquidity import detect_liquidity_levels
from src.features.sessions import compute_sessions
from src.features.reference_levels import compute_reference_levels, compute_weekend_gaps
from src.features.engulfing import detect_engulfing
from src.features.confluence import generate_confluence_dataset
from src.features.storage import save_feature_dataset


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--timeframe", default="15min")
    ap.add_argument("--source-tz", default="UTC")
    ap.add_argument("--left", type=int, default=2)
    ap.add_argument("--right", type=int, default=2)
    ap.add_argument("--confluence-stride", type=int, default=100)
    ap.add_argument("--out-dir", default="data/processed")
    args = ap.parse_args()

    m1, report = load_m1_csv(args.input, source_tz=args.source_tz)
    print(report.summary())

    candles = resample_ohlc(m1, args.timeframe) if args.timeframe != "1min" else m1
    candles = candles.reset_index(drop=True)

    swing_cfg = SwingConfig(left=args.left, right=args.right)
    structure_cfg = StructureConfig(swing=swing_cfg)
    swings = detect_swings(candles, config=swing_cfg, timeframe_label=args.timeframe)
    events = detect_structure_events(candles, swings, symbol=args.symbol, timeframe=args.timeframe, config=structure_cfg)

    order_blocks, skipped = detect_order_blocks(
        candles, args.symbol, args.timeframe, config=OrderBlockConfig(), structure_events=events,
    )
    fvgs = detect_fvgs(candles, args.symbol, args.timeframe, config=FVGConfig())
    liquidity = detect_liquidity_levels(
        candles, args.symbol, args.timeframe, config=LiquidityConfig(swing_left=args.left, swing_right=args.right),
    )
    sessions = compute_sessions(m1)
    reference_levels = compute_reference_levels(m1)
    weekend_gaps = compute_weekend_gaps(m1)
    engulfing = detect_engulfing(candles)
    confluence = generate_confluence_dataset(candles, args.symbol, args.timeframe, stride=args.confluence_stride)

    out_dir = Path(args.out_dir)
    save_feature_dataset(order_blocks, out_dir / "order_blocks.parquet")
    save_feature_dataset(fvgs, out_dir / "fvgs.parquet")
    save_feature_dataset(liquidity, out_dir / "liquidity.parquet")
    save_feature_dataset(sessions, out_dir / "sessions.parquet")
    save_feature_dataset(reference_levels, out_dir / "reference_levels.parquet")
    save_feature_dataset(weekend_gaps, out_dir / "weekend_gaps.parquet")
    save_feature_dataset(engulfing, out_dir / "engulfing.parquet")
    save_feature_dataset(confluence, out_dir / "confluence.parquet")

    print(f"order_blocks: {len(order_blocks)} ({len(skipped)} displacement events skipped -- no opposite candle found)")
    print(f"fvgs: {len(fvgs)}")
    print(f"liquidity: {len(liquidity)}")
    print(f"sessions: {len(sessions)}")
    print(f"reference_levels: {len(reference_levels)}")
    print(f"weekend_gaps: {len(weekend_gaps)}")
    print(f"engulfing: {len(engulfing)}")
    print(f"confluence: {len(confluence)}")
    print(f"Datasets written to {out_dir.resolve()}")


if __name__ == "__main__":
    main()
