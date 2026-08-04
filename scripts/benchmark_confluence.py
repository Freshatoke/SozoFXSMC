"""
Benchmark: Task 2 request-time confluence recomputation (O(n) per snapshot,
O(n^2) total over a full run) vs the Task 2.5 incremental engine (O(n)
total, O(active-objects) per candle).

Usage:
    python scripts/benchmark_confluence.py --num-candles 600 --incremental-only-candles 20000
"""

from __future__ import annotations

import argparse
import sys
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from src.features.confluence import build_confluence_snapshot
from src.engine.engine import IncrementalEngine


def make_synthetic_candles(n: int, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    ts = pd.date_range("2024-01-01 00:00:00", periods=n, freq="1min", tz="UTC")
    returns = rng.normal(0, 0.0003, n)
    trend = np.sin(np.linspace(0, 8 * np.pi, n)) * 0.001
    close = 1.1000 + np.cumsum(returns + np.diff(np.concatenate([[0], trend])))
    open_ = np.concatenate([[1.1000], close[:-1]])
    high = np.maximum(open_, close) + np.abs(rng.normal(0, 0.0002, n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0, 0.0002, n))
    return pd.DataFrame({"timestamp": ts, "open": open_, "high": high, "low": low, "close": close})


def benchmark_old(df: pd.DataFrame, symbol="BENCH", timeframe="M1", every: int = 1) -> dict:
    tracemalloc.start()
    t0 = time.perf_counter()
    n_snapshots = 0
    for i in range(0, len(df), every):
        build_confluence_snapshot(df, i, symbol, timeframe)
        n_snapshots += 1
    elapsed = time.perf_counter() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {"elapsed_sec": elapsed, "peak_memory_mb": peak / (1024 * 1024), "snapshots": n_snapshots,
            "avg_per_candle_ms": (elapsed / n_snapshots) * 1000 if n_snapshots else 0.0}


def benchmark_incremental(df: pd.DataFrame, symbol="BENCH", timeframe="M1") -> dict:
    tracemalloc.start()
    t0 = time.perf_counter()
    engine = IncrementalEngine(symbol=symbol, timeframe=timeframe, interval=pd.Timedelta(minutes=1))
    engine.process_dataframe(df)
    elapsed = time.perf_counter() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "elapsed_sec": elapsed, "peak_memory_mb": peak / (1024 * 1024),
        "candles_processed": engine.candles_processed,
        "avg_per_candle_ms": (elapsed / engine.candles_processed) * 1000 if engine.candles_processed else 0.0,
        "order_blocks": len(engine.order_blocks.all_order_blocks),
        "fvgs": len(engine.fvgs.all_fvgs),
        "liquidity_levels": len(engine.liquidity.all_levels),
        "structure_events": len(engine.structure.events),
        "events_published": len(engine.event_bus.log),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--num-candles", type=int, default=600, help="Size of the head-to-head comparison run (old is O(n^2), keep this modest)")
    ap.add_argument("--incremental-only-candles", type=int, default=20000, help="Larger run to show the incremental engine's O(n) scaling")
    args = ap.parse_args()

    print(f"=== Head-to-head comparison: {args.num_candles} candles ===")
    df_small = make_synthetic_candles(args.num_candles)

    old_result = benchmark_old(df_small)
    print("Old (request-time, recompute-from-scratch per snapshot):")
    for k, v in old_result.items():
        print(f"  {k}: {v}")

    new_result = benchmark_incremental(df_small)
    print("New (incremental engine):")
    for k, v in new_result.items():
        print(f"  {k}: {v}")

    speedup = old_result["elapsed_sec"] / new_result["elapsed_sec"] if new_result["elapsed_sec"] > 0 else float("inf")
    print(f"\nSpeedup at n={args.num_candles}: {speedup:.1f}x")
    print(f"Old avg/candle: {old_result['avg_per_candle_ms']:.3f} ms  |  New avg/candle: {new_result['avg_per_candle_ms']:.3f} ms")

    print(f"\n=== Incremental-only scaling run: {args.incremental_only_candles} candles ===")
    df_big = make_synthetic_candles(args.incremental_only_candles)
    big_result = benchmark_incremental(df_big)
    for k, v in big_result.items():
        print(f"  {k}: {v}")
    scale = (args.incremental_only_candles / args.num_candles) ** 2
    extrapolated_old_seconds = old_result["elapsed_sec"] * scale
    print(
        f"\nExtrapolating the old approach's measured O(n^2) scaling from n={args.num_candles} to "
        f"n={args.incremental_only_candles} (factor of {scale:.0f}x) gives an estimated "
        f"{extrapolated_old_seconds:.1f} seconds -- not run directly here because it would take far too long."
    )


if __name__ == "__main__":
    main()
