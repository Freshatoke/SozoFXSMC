"""Task 9 Phase 1 driver: builds the master S3/S4 feature dataset from
Task 8's cached per-symbol Tier 1 results (trades + MarketContext)."""

from __future__ import annotations

import pickle
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.research.trade_features import build_master_feature_dataset
from src.features.storage import save_feature_dataset

CACHE_DIR = ROOT / "reports" / "institutional_research" / "_cache"
OUT_DIR = ROOT / "reports" / "edge_refinement"


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    trades_by_symbol = {}
    for path in sorted(CACHE_DIR.glob("*_tier1.pkl")):
        symbol = path.name.replace("_tier1.pkl", "")
        with open(path, "rb") as f:
            trades_by_symbol[symbol] = pickle.load(f)
        print(f"Loaded {symbol}")

    df = build_master_feature_dataset(trades_by_symbol, strategy_ids=("S3", "S4"))
    print(f"Master feature dataset: {len(df)} rows, {len(df.columns)} columns")
    print(df["strategy_id"].value_counts())
    print(df["is_closed"].value_counts())

    save_feature_dataset(df, OUT_DIR / "master_feature_dataset.parquet", index=False)
    print(f"Wrote {OUT_DIR / 'master_feature_dataset.parquet'}")


if __name__ == "__main__":
    main()
