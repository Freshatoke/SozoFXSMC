"""
Task 7.4 -- Behavior-preservation harness (Objective 3).

Captures a "golden" snapshot of pipeline output (signals, trades,
metrics) on a fixed real-data slice, and compares two snapshots for
EXACT equality. Every optimization in this task must produce a snapshot
identical to the pre-optimization golden snapshot -- if it doesn't, the
optimization is rejected per the task's Objective 3.

Usage:
    python scripts/golden_snapshot.py capture --input data/raw/EURUSD_M1_histdata.parquet \
        --symbol EURUSD --size 1mo --out reports/performance/golden/before.json

    python scripts/golden_snapshot.py capture ... --out reports/performance/golden/after.json

    python scripts/golden_snapshot.py compare \
        reports/performance/golden/before.json reports/performance/golden/after.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from src.strategies.context import MarketContext
from src.strategies.runner import run_strategies
from src.backtest.engine import run_backtest
from src.backtest.metrics import compute_performance_metrics
from src.research.market_conditions import classify_market_conditions, label_trades_with_conditions
from scripts.benchmark_scaling import slice_m1


def _json_default(obj):
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return str(obj)


def _stable_dump(obj) -> str:
    return json.dumps(obj, sort_keys=True, default=_json_default)


def capture_snapshot(m1: pd.DataFrame, symbol: str) -> dict:
    context = MarketContext(symbol=symbol, m1=m1)
    signals = run_strategies(context)
    trades = run_backtest(signals, m1, context=context)
    metrics = compute_performance_metrics(trades, starting_balance=10_000.0)
    conditions = classify_market_conditions(m1)
    label_trades_with_conditions(trades, conditions)

    signal_rows = [
        {
            "signal_id": s.signal_id, "strategy_id": s.strategy_id, "timestamp": s.timestamp,
            "direction": s.direction, "confidence_score": s.confidence_score,
            "entry_zone": list(s.entry_zone), "reason_codes": s.reason_codes,
        }
        for s in signals
    ]
    trade_rows = [
        {
            "trade_id": t.trade_id, "signal_id": t.signal_id, "status": t.status,
            "entry_price": t.entry_price, "entry_timestamp": t.entry_timestamp,
            "exit_price": t.exit_price, "exit_timestamp": t.exit_timestamp, "exit_reason": t.exit_reason,
            "realized_pnl": t.realized_pnl, "r_multiple": t.r_multiple,
            "duration_candles": t.duration_candles, "mae": t.mae, "mfe": t.mfe,
            "trend_state": t.metadata.get("trend_state"), "volatility_state": t.metadata.get("volatility_state"),
        }
        for t in trades
    ]

    return {
        "num_candles": len(m1),
        "signals": sorted(signal_rows, key=lambda r: r["signal_id"]),
        "trades": sorted(trade_rows, key=lambda r: r["trade_id"]),
        "metrics": metrics,
    }


def capture(args: argparse.Namespace) -> int:
    m1_full = pd.read_parquet(args.input)
    m1_full = m1_full[["timestamp", "open", "high", "low", "close"]].reset_index(drop=True)
    m1 = slice_m1(m1_full, args.size)
    print(f"Capturing snapshot over {len(m1)} candles ({args.size}) for {args.symbol}...")

    snapshot = capture_snapshot(m1, args.symbol)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(_stable_dump(snapshot))
    print(f"Wrote {len(snapshot['signals'])} signals, {len(snapshot['trades'])} trades to {out_path}")
    return 0


def compare(args: argparse.Namespace) -> int:
    left = json.loads(Path(args.left).read_text())
    right = json.loads(Path(args.right).read_text())

    identical = True
    if left["num_candles"] != right["num_candles"]:
        print(f"MISMATCH: num_candles {left['num_candles']} != {right['num_candles']}")
        identical = False

    if _stable_dump(left["signals"]) != _stable_dump(right["signals"]):
        print(f"MISMATCH: signals differ ({len(left['signals'])} vs {len(right['signals'])})")
        identical = False
        for a, b in zip(left["signals"], right["signals"]):
            if _stable_dump(a) != _stable_dump(b):
                print(f"  first diverging signal:\n    before: {a}\n    after:  {b}")
                break

    if _stable_dump(left["trades"]) != _stable_dump(right["trades"]):
        print(f"MISMATCH: trades differ ({len(left['trades'])} vs {len(right['trades'])})")
        identical = False
        for a, b in zip(left["trades"], right["trades"]):
            if _stable_dump(a) != _stable_dump(b):
                print(f"  first diverging trade:\n    before: {a}\n    after:  {b}")
                break

    if _stable_dump(left["metrics"]) != _stable_dump(right["metrics"]):
        print("MISMATCH: metrics differ")
        identical = False
        for key in left["metrics"]:
            if _stable_dump(left["metrics"].get(key)) != _stable_dump(right["metrics"].get(key)):
                print(f"  metric '{key}': before={left['metrics'].get(key)} after={right['metrics'].get(key)}")

    if identical:
        print("IDENTICAL: signals, trades, and metrics match exactly.")
        return 0
    return 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    cap = sub.add_parser("capture")
    cap.add_argument("--input", default="data/raw/EURUSD_M1_histdata.parquet")
    cap.add_argument("--symbol", default="EURUSD")
    cap.add_argument("--size", default="1mo")
    cap.add_argument("--out", required=True)

    cmp_parser = sub.add_parser("compare")
    cmp_parser.add_argument("left")
    cmp_parser.add_argument("right")

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "capture":
        return capture(args)
    return compare(args)


if __name__ == "__main__":
    raise SystemExit(main())
