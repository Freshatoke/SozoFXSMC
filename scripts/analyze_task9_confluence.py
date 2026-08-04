"""
Task 9 Phase 3 — Confluence Discovery.

Tests specific, named combinations of the features found significant in
Phase 2 (Order Block freshness, Order Block quality, session, PDH
distance) plus the task brief's own worked example, and reports
expectancy/win-rate/profit-factor/sample-size for each combination so
"is this combination just a small lucky sample" is visible directly in
the table (a large expectancy on n=4 trades is flagged, not hidden).
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.features.storage import save_feature_dataset

OUT_DIR = ROOT / "reports" / "edge_refinement"


def _metrics_for(sub: pd.DataFrame) -> dict:
    closed = sub[sub.is_closed == True]  # noqa: E712
    if closed.empty:
        return {"n": 0, "win_rate": None, "expectancy_r": None, "profit_factor": None}
    pnls = closed["realized_pnl"].to_numpy()
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]
    gross_profit = wins.sum() if len(wins) else 0.0
    gross_loss = losses.sum() if len(losses) else 0.0
    return {
        "n": len(closed),
        "win_rate": round(len(wins) / len(closed), 4),
        "expectancy_r": round(float(closed["r_multiple"].mean()), 4),
        "profit_factor": round(gross_profit / abs(gross_loss), 4) if gross_loss != 0 else (float("inf") if gross_profit > 0 else 0.0),
    }


def evaluate_combination(df: pd.DataFrame, strategy_id: str, name: str, mask: pd.Series) -> dict:
    sub = df[(df.strategy_id == strategy_id) & mask]
    m = _metrics_for(sub)
    return {"strategy_id": strategy_id, "combination": name, **m}


def main() -> None:
    df = pd.read_parquet(OUT_DIR / "master_feature_dataset.parquet")
    rows = []

    for sid in ["S3", "S4"]:
        s = df[df.strategy_id == sid]
        rows.append(evaluate_combination(df, sid, "ALL (baseline)", pd.Series(True, index=df.index)))

        # Phase 2 top finding: OB freshness x quality
        median_q = s["ob_quality_score"].median()
        rows.append(evaluate_combination(df, sid, "FreshOB + HighQuality(>median)",
                                          (df.ob_freshness_status == "FRESH") & (df.ob_quality_score > median_q)))
        rows.append(evaluate_combination(df, sid, "FreshOB + LowQuality(<=median)",
                                          (df.ob_freshness_status == "FRESH") & (df.ob_quality_score <= median_q)))
        rows.append(evaluate_combination(df, sid, "MitigatedOB (any quality)",
                                          df.ob_freshness_status == "MITIGATED"))

        # Task brief's worked example: CHoCH + Fresh OB + London + Sweep, vs CHoCH + Weak(mitigated) OB + Asian
        rows.append(evaluate_combination(df, sid, "CHoCH + FreshOB + London + LiquidityRef",
                                          df.has_choch & (df.ob_freshness_status == "FRESH") & (df.session == "london") & df.has_liquidity_ref))
        rows.append(evaluate_combination(df, sid, "CHoCH + MitigatedOB + Asian(tokyo)",
                                          df.has_choch & (df.ob_freshness_status == "MITIGATED") & (df.session == "tokyo")))

        # Session x OB freshness
        for session in ["london", "new_york", "tokyo", "sydney"]:
            rows.append(evaluate_combination(df, sid, f"FreshOB + Session={session}",
                                              (df.ob_freshness_status == "FRESH") & (df.session == session)))

        # PDH distance tercile (S3's other significant feature) x freshness
        pdh_tercile = s["pdh_distance_pips"].quantile([0.33, 0.66]).values if s["pdh_distance_pips"].notna().sum() > 3 else [np.nan, np.nan]
        if not np.isnan(pdh_tercile[0]):
            rows.append(evaluate_combination(df, sid, "FreshOB + Near PDH/PDL (<33rd pct)",
                                              (df.ob_freshness_status == "FRESH") & (df.pdh_distance_pips < pdh_tercile[0])))
            rows.append(evaluate_combination(df, sid, "FreshOB + Far from PDH/PDL (>66th pct)",
                                              (df.ob_freshness_status == "FRESH") & (df.pdh_distance_pips > pdh_tercile[1])))

        # Displacement confirmation x freshness (S3-specific factor, tested for both for comparison)
        rows.append(evaluate_combination(df, sid, "FreshOB + DisplacementConfirmed",
                                          (df.ob_freshness_status == "FRESH") & df.has_displacement_confirmed))

    out = pd.DataFrame(rows)
    out["baseline_expectancy_r"] = out.groupby("strategy_id")["expectancy_r"].transform("first")
    out["expectancy_delta_vs_baseline"] = (out["expectancy_r"] - out["baseline_expectancy_r"]).round(4)
    out["reliable_sample"] = out["n"] >= 30  # flag, not filter -- small samples stay visible but marked

    save_feature_dataset(out, OUT_DIR / "confluence_report.parquet", index=False)
    out.to_csv(OUT_DIR / "confluence_report.csv", index=False)
    print(out.to_string(index=False))
    print(f"\nWrote {OUT_DIR / 'confluence_report.parquet'} and .csv")


if __name__ == "__main__":
    main()
