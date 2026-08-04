"""Task 10 Phase 8 driver: runs paper trading across all available
symbols' cached S3/S4 trades and saves the full report set."""

from __future__ import annotations

import pickle
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.decision_engine.paper_trading import run_paper_trading
from src.features.storage import save_feature_dataset

CACHE_DIR = ROOT / "reports" / "institutional_research" / "_cache"
OUT_DIR = ROOT / "reports" / "decision_engine"


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    trades_by_symbol = {}
    for path in sorted(CACHE_DIR.glob("*_tier1.pkl")):
        symbol = path.name.replace("_tier1.pkl", "")
        print(f"Loading {symbol}...", flush=True)
        with open(path, "rb") as f:
            trades_by_symbol[symbol] = pickle.load(f)

    print("Running paper trading simulation across all symbols...", flush=True)
    result = run_paper_trading(trades_by_symbol)

    save_feature_dataset(result.decisions_log, OUT_DIR / "paper_trading_decisions.parquet", index=False)
    result.daily_report.to_csv(OUT_DIR / "paper_trading_daily_report.csv", index=False)
    result.weekly_report.to_csv(OUT_DIR / "paper_trading_weekly_report.csv", index=False)

    print("\n=== SELECTED (decision engine) vs BASELINE (take everything) ===")
    print("Selected: ", result.selected_summary)
    print("Baseline: ", result.baseline_summary)

    import json
    with open(OUT_DIR / "paper_trading_summary.json", "w") as f:
        json.dump({"selected": result.selected_summary, "baseline": result.baseline_summary}, f, indent=2)

    print(f"\nWrote reports to {OUT_DIR}")


if __name__ == "__main__":
    main()
