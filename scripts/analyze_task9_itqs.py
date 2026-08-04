"""Task 9 Phase 4 driver: computes ITQS for every S3/S4 trade and
validates it exactly the way Task 8 validated the existing confidence
score (Spearman correlation vs. outcome, bucket performance table)."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.research.itqs import add_itqs_column, itqs_bucket
from src.features.storage import save_feature_dataset

OUT_DIR = ROOT / "reports" / "edge_refinement"


def main() -> None:
    df = pd.read_parquet(OUT_DIR / "master_feature_dataset.parquet")
    df = add_itqs_column(df)
    df["itqs_bucket"] = df["itqs"].apply(itqs_bucket)

    save_feature_dataset(df, OUT_DIR / "master_feature_dataset_with_itqs.parquet", index=False)

    rows = []
    for sid in ["S3", "S4"]:
        closed = df[(df.strategy_id == sid) & (df.is_closed == True)]  # noqa: E712
        corr_pnl = closed["itqs"].corr(closed["realized_pnl"], method="spearman")
        corr_r = closed["itqs"].corr(closed["r_multiple"], method="spearman")
        print(f"=== {sid}: ITQS vs outcome ===")
        print(f"  Spearman(ITQS, realized_pnl) = {corr_pnl:.4f}")
        print(f"  Spearman(ITQS, r_multiple)   = {corr_r:.4f}")

        bucket_perf = closed.groupby("itqs_bucket").agg(
            n=("trade_id", "count"), win_rate=("is_winner", "mean"), expectancy_r=("r_multiple", "mean"),
        ).reset_index()
        bucket_perf.insert(0, "strategy_id", sid)
        print(bucket_perf.to_string(index=False))
        print()
        rows.append(bucket_perf)

    bucket_summary = pd.concat(rows, ignore_index=True)
    save_feature_dataset(bucket_summary, OUT_DIR / "itqs_bucket_performance.parquet", index=False)
    bucket_summary.to_csv(OUT_DIR / "itqs_bucket_performance.csv", index=False)
    print(f"Wrote master_feature_dataset_with_itqs.parquet and itqs_bucket_performance.parquet/.csv")


if __name__ == "__main__":
    main()
