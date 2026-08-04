"""
Task 7.4 -- Scaling benchmark utility.

Runs the pipeline (MarketContext + strategies -> backtest -> market
conditions) on increasing real-data slice sizes and reports wall-clock
time, peak RSS memory, CPU usage, and candles/sec per size -- this is
the single source of truth for "is scaling roughly linear" used by
Objective 1 (baseline), every Objective 2 before/after measurement, and
Objective 4 (the final scaling report).

Usage:
    python scripts/benchmark_scaling.py \
        --input data/raw/EURUSD_M1_histdata.parquet --symbol EURUSD \
        --sizes 1mo,3mo,1yr,3yr,full \
        --out reports/performance/scaling_benchmark.parquet \
        --profile --profile-dir reports/performance/profiles
"""

from __future__ import annotations

import argparse
import cProfile
import json
import pstats
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from src.strategies.context import MarketContext
from src.strategies.runner import run_strategies
from src.backtest.engine import run_backtest
from src.research.market_conditions import classify_market_conditions, label_trades_with_conditions
from src.utils.perf import ResourceMonitor, candles_per_second

SIZE_DAYS = {
    "1mo": 30,
    "3mo": 91,
    "6mo": 182,
    "1yr": 365,
    "3yr": 365 * 3,
    "full": None,
}


def slice_m1(m1: pd.DataFrame, size: str) -> pd.DataFrame:
    if size == "full" or SIZE_DAYS[size] is None:
        return m1.reset_index(drop=True)
    start = m1["timestamp"].iloc[0]
    end = start + pd.Timedelta(days=SIZE_DAYS[size])
    return m1[(m1["timestamp"] >= start) & (m1["timestamp"] < end)].reset_index(drop=True)


def run_pipeline_stages(m1: pd.DataFrame, symbol: str) -> dict:
    """Runs the exact 3 stages profiled in Task 7.3 so results stay
    directly comparable across the whole optimization effort. Returns
    per-stage wall time plus signal/trade counts."""
    stage_times = {}

    with ResourceMonitor() as mon_ctx:
        context = MarketContext(symbol=symbol, m1=m1)
        signals = run_strategies(context)
    stage_times["strategies"] = mon_ctx.usage.to_dict()

    with ResourceMonitor() as mon_bt:
        trades = run_backtest(signals, m1, context=context)
    stage_times["backtest"] = mon_bt.usage.to_dict()

    with ResourceMonitor() as mon_mc:
        conditions = classify_market_conditions(m1)
        label_trades_with_conditions(trades, conditions)
    stage_times["market_conditions"] = mon_mc.usage.to_dict()

    return {
        "num_signals": len(signals),
        "num_trades": len(trades),
        "stage_times": stage_times,
    }


def run_one_size(m1_full: pd.DataFrame, symbol: str, size: str, profile: bool, profile_dir: Path | None) -> dict:
    m1 = slice_m1(m1_full, size)
    num_candles = len(m1)
    if num_candles == 0:
        return {"size": size, "num_candles": 0, "skipped": True}

    profiler = cProfile.Profile() if profile else None
    if profiler:
        profiler.enable()

    with ResourceMonitor() as mon_total:
        result = run_pipeline_stages(m1, symbol)
    total_usage = mon_total.usage.to_dict()

    if profiler:
        profiler.disable()
        profile_dir.mkdir(parents=True, exist_ok=True)
        stats_path = profile_dir / f"profile_{size}.txt"
        with open(stats_path, "w") as fh:
            st = pstats.Stats(profiler, stream=fh)
            st.sort_stats("cumulative")
            st.print_stats(40)
            fh.write("\n\n--- BY TOTTIME ---\n")
            st.sort_stats("tottime")
            st.print_stats(40)
        profiler.dump_stats(str(profile_dir / f"profile_{size}.prof"))

    row = {
        "size": size,
        "num_candles": num_candles,
        "num_signals": result["num_signals"],
        "num_trades": result["num_trades"],
        "wall_seconds": total_usage["wall_seconds"],
        "cpu_seconds": total_usage["cpu_seconds"],
        "peak_rss_mb": total_usage["peak_rss_mb"],
        "candles_per_sec": candles_per_second(num_candles, total_usage["wall_seconds"]),
        "stage_times": json.dumps(result["stage_times"]),
    }
    return row


def assess_linearity(results: list[dict]) -> list[dict]:
    """For each consecutive pair of sizes, ratio of (time growth) to
    (candle-count growth). ~1.0 = linear; > 1.0 = worse-than-linear."""
    usable = [r for r in results if not r.get("skipped")]
    for i in range(1, len(usable)):
        prev, cur = usable[i - 1], usable[i]
        candle_ratio = cur["num_candles"] / prev["num_candles"] if prev["num_candles"] else float("nan")
        time_ratio = cur["wall_seconds"] / prev["wall_seconds"] if prev["wall_seconds"] else float("nan")
        cur["scaling_factor_vs_prev"] = round(time_ratio / candle_ratio, 3) if candle_ratio else None
    return usable


def _markdown_table(df: pd.DataFrame) -> str:
    cols = list(df.columns)
    header = "| " + " | ".join(cols) + " |"
    sep = "| " + " | ".join(["---"] * len(cols)) + " |"
    rows = ["| " + " | ".join(str(v) for v in row) + " |" for row in df[cols].itertuples(index=False)]
    return "\n".join([header, sep, *rows])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="data/raw/EURUSD_M1_histdata.parquet")
    parser.add_argument("--symbol", default="EURUSD")
    parser.add_argument("--sizes", default="1mo,3mo,1yr,3yr,full")
    parser.add_argument("--out", default="reports/performance/scaling_benchmark.parquet")
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--profile-dir", default="reports/performance/profiles")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    m1_full = pd.read_parquet(args.input)
    m1_full = m1_full[["timestamp", "open", "high", "low", "close"]].reset_index(drop=True)
    print(f"Loaded {len(m1_full)} candles from {args.input} ({m1_full['timestamp'].iloc[0]} to {m1_full['timestamp'].iloc[-1]})")

    sizes = args.sizes.split(",")
    profile_dir = Path(args.profile_dir) if args.profile else None
    results = []
    for size in sizes:
        print(f"\n=== Running size: {size} ===")
        row = run_one_size(m1_full, args.symbol, size, args.profile, profile_dir)
        print(row)
        results.append(row)

    results = assess_linearity(results)
    df = pd.DataFrame(results)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out_path, index=False)
    print(f"\nSaved benchmark results to {out_path}")

    md_path = out_path.with_suffix(".md")
    lines = ["# Scaling Benchmark", "", _markdown_table(df.drop(columns=["stage_times"]))]
    md_path.write_text("\n".join(lines))
    print(f"Saved markdown summary to {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
