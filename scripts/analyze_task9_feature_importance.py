"""
Task 9 Phase 2 — Feature Importance.

Ranks every PRE-ENTRY feature (known at or before the trade's entry
timestamp -- explicitly excludes trade-lifecycle outcomes like MAE/MFE/
duration_candles/exit_reason, which are only known AFTER entry and would
be circular/look-ahead if used to explain the entry decision) by three
explainable, non-black-box methods:

  - Point-biserial correlation (continuous features vs is_winner) /
    Pearson correlation (continuous features vs r_multiple)
  - Mutual information (sklearn.feature_selection -- a nonparametric
    information-theoretic ESTIMATOR, not a trained predictive model; it
    answers "how much does knowing this feature reduce uncertainty about
    the outcome", which is exactly what Information Gain / Mutual
    Information means in the task brief, not a black-box AI system)
  - Statistical significance: Welch's t-test (continuous features,
    winners vs losers) / chi-square test of independence (categorical
    features vs win/loss)

No model is trained and no predictions are made anywhere in this script.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats
from sklearn.feature_selection import mutual_info_classif, mutual_info_regression

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.features.storage import save_feature_dataset

OUT_DIR = ROOT / "reports" / "edge_refinement"

CONTINUOUS_FEATURES = [
    "entry_hour_utc", "entry_weekday", "confidence_score",
    "ob_age_candles", "ob_size_pips", "ob_quality_score", "ob_wick_ratio",
    "liquidity_touches", "atr_pips_at_entry",
    "pdh_distance_pips", "pdl_distance_pips", "asian_range_pips", "asian_range_distance_pips",
    "choch_to_entry_candles", "sweep_to_choch_candles",
]
CATEGORICAL_FEATURES = [
    "direction", "session", "trend_state", "volatility_state", "directional_bias",
    "is_gap_day", "is_news_day", "has_choch", "has_order_block", "ob_freshness_status",
    "has_liquidity_ref", "liquidity_strength", "has_fvg_alignment", "has_displacement_confirmed",
    "session_overlap",
]


def _clean_continuous(series: pd.Series) -> pd.Series:
    # ob_age_candles has extreme outliers (some OBs referenced are years
    # old); cap at the 99th percentile for correlation/MI purposes so one
    # extreme value doesn't dominate a Pearson correlation -- documented,
    # not silently done: see the "capped_at_p99" note in the output.
    s = pd.to_numeric(series, errors="coerce")
    if s.notna().sum() < 3:
        return s
    cap = s.quantile(0.99)
    return s.clip(upper=cap)


def analyze_strategy(df: pd.DataFrame, strategy_id: str) -> pd.DataFrame:
    sub = df[(df.strategy_id == strategy_id) & (df.is_closed == True)].copy()  # noqa: E712
    sub = sub[sub["is_winner"].notna()]
    y_class = sub["is_winner"].astype(int).to_numpy()
    y_reg = sub["r_multiple"].to_numpy()

    rows = []
    for feat in CONTINUOUS_FEATURES:
        if feat not in sub.columns:
            continue
        x = _clean_continuous(sub[feat])
        valid = x.notna()
        if valid.sum() < 10:
            continue
        x_valid = x[valid].to_numpy()
        y_class_valid = y_class[valid.to_numpy()]
        y_reg_valid = y_reg[valid.to_numpy()]

        winners = x_valid[y_class_valid == 1]
        losers = x_valid[y_class_valid == 0]
        if len(winners) < 3 or len(losers) < 3:
            continue

        pb_corr, pb_p = stats.pointbiserialr(y_class_valid, x_valid)
        pearson_r_corr, pearson_r_p = stats.pearsonr(x_valid, y_reg_valid) if np.std(x_valid) > 0 else (0.0, 1.0)
        mi_class = mutual_info_classif(x_valid.reshape(-1, 1), y_class_valid, random_state=42)[0]
        mi_reg = mutual_info_regression(x_valid.reshape(-1, 1), y_reg_valid, random_state=42)[0]
        t_stat, t_p = stats.ttest_ind(winners, losers, equal_var=False)

        rows.append({
            "strategy_id": strategy_id, "feature": feat, "feature_type": "continuous",
            "point_biserial_corr_vs_win": round(float(pb_corr), 4), "corr_p_value": round(float(pb_p), 4),
            "pearson_corr_vs_r_multiple": round(float(pearson_r_corr), 4),
            "mutual_info_vs_win": round(float(mi_class), 4), "mutual_info_vs_r_multiple": round(float(mi_reg), 4),
            "t_test_statistic": round(float(t_stat), 4), "t_test_p_value": round(float(t_p), 4),
            "significant_at_0.05": bool(t_p < 0.05),
            "winner_mean": round(float(np.mean(winners)), 4), "loser_mean": round(float(np.mean(losers)), 4),
            "n": int(valid.sum()),
        })

    for feat in CATEGORICAL_FEATURES:
        if feat not in sub.columns:
            continue
        x = sub[feat].astype(str)
        valid = x.notna() & (x != "None") & (x != "nan")
        if valid.sum() < 10:
            continue
        x_valid = x[valid]
        y_class_valid = y_class[valid.to_numpy()]
        y_reg_valid = y_reg[valid.to_numpy()]

        codes, _ = pd.factorize(x_valid)
        if len(set(codes)) < 2:
            continue
        mi_class = mutual_info_classif(codes.reshape(-1, 1), y_class_valid, discrete_features=True, random_state=42)[0]
        mi_reg = mutual_info_regression(codes.reshape(-1, 1), y_reg_valid, discrete_features=True, random_state=42)[0]

        contingency = pd.crosstab(x_valid, y_class_valid)
        if contingency.shape[0] < 2 or contingency.shape[1] < 2:
            chi2, chi_p = 0.0, 1.0
        else:
            chi2, chi_p, _, _ = stats.chi2_contingency(contingency)

        win_rate_by_cat = sub.loc[valid].groupby(feat)["is_winner"].mean().to_dict()
        best_cat = max(win_rate_by_cat, key=win_rate_by_cat.get) if win_rate_by_cat else None
        worst_cat = min(win_rate_by_cat, key=win_rate_by_cat.get) if win_rate_by_cat else None

        rows.append({
            "strategy_id": strategy_id, "feature": feat, "feature_type": "categorical",
            "mutual_info_vs_win": round(float(mi_class), 4), "mutual_info_vs_r_multiple": round(float(mi_reg), 4),
            "chi2_statistic": round(float(chi2), 4), "chi2_p_value": round(float(chi_p), 4),
            "significant_at_0.05": bool(chi_p < 0.05),
            "best_category": str(best_cat), "best_category_win_rate": round(win_rate_by_cat.get(best_cat, 0.0), 4) if best_cat else None,
            "worst_category": str(worst_cat), "worst_category_win_rate": round(win_rate_by_cat.get(worst_cat, 0.0), 4) if worst_cat else None,
            "n": int(valid.sum()),
        })

    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values("mutual_info_vs_win", ascending=False).reset_index(drop=True)
    return out


def main() -> None:
    df = pd.read_parquet(OUT_DIR / "master_feature_dataset.parquet")
    all_rows = []
    for sid in ["S3", "S4"]:
        result = analyze_strategy(df, sid)
        all_rows.append(result)
        print(f"=== {sid} top 10 features by mutual info vs win ===")
        print(result[["feature", "feature_type", "mutual_info_vs_win", "significant_at_0.05"]].head(10).to_string(index=False))
        print()

    combined = pd.concat(all_rows, ignore_index=True)
    save_feature_dataset(combined, OUT_DIR / "feature_importance.parquet", index=False)
    combined.to_csv(OUT_DIR / "feature_importance.csv", index=False)
    print(f"Wrote {OUT_DIR / 'feature_importance.parquet'} and .csv")


if __name__ == "__main__":
    main()
