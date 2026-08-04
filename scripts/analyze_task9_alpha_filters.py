"""
Task 9 Phase 10 — Alpha Filter Search.

Every filter is evaluated POST-HOC (removing trades from the already-
generated S3/S4 trade set, exactly like `src.research.filter_analysis` --
this doesn't need new backtests, just group comparisons over the master
feature dataset from Phase 1). A filter is ACCEPTED only if it improves
at least one of {expectancy, profit factor, drawdown, recovery factor}
on the WITH side relative to the unfiltered baseline; otherwise it is
explicitly REJECTED and recorded as rejected, not silently dropped.
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


def _drawdown_pct(pnls: np.ndarray, starting_balance: float = 10_000.0) -> float:
    equity = starting_balance + np.cumsum(pnls)
    peak = np.maximum.accumulate(np.concatenate([[starting_balance], equity]))[1:]
    dd = (equity - peak) / peak
    return float(dd.min()) if len(dd) else 0.0


def _metrics(sub: pd.DataFrame) -> dict:
    closed = sub[sub.is_closed == True]  # noqa: E712
    if closed.empty:
        return {"n": 0, "expectancy_r": None, "profit_factor": None, "max_drawdown_pct": None, "recovery_factor": None}
    pnls = closed["realized_pnl"].to_numpy()
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]
    gross_profit = wins.sum() if len(wins) else 0.0
    gross_loss = losses.sum() if len(losses) else 0.0
    net_profit = gross_profit + gross_loss
    dd = _drawdown_pct(pnls)
    return {
        "n": len(closed),
        "expectancy_r": round(float(closed["r_multiple"].mean()), 4),
        "profit_factor": round(gross_profit / abs(gross_loss), 4) if gross_loss != 0 else (float("inf") if gross_profit > 0 else 0.0),
        "max_drawdown_pct": round(dd * 100, 2),
        "recovery_factor": round(net_profit / abs(dd * 10_000), 4) if dd != 0 else (float("inf") if net_profit > 0 else 0.0),
    }


def evaluate_filter(df: pd.DataFrame, strategy_id: str, name: str, mask: pd.Series) -> dict:
    sub = df[df.strategy_id == strategy_id]
    baseline = _metrics(sub)
    with_filter = _metrics(sub[mask])
    without_filter = _metrics(sub[~mask])

    improves = []
    if with_filter["n"] and baseline["n"]:
        if with_filter["expectancy_r"] is not None and baseline["expectancy_r"] is not None and with_filter["expectancy_r"] > baseline["expectancy_r"]:
            improves.append("expectancy")
        if with_filter["profit_factor"] is not None and baseline["profit_factor"] is not None and with_filter["profit_factor"] > baseline["profit_factor"]:
            improves.append("profit_factor")
        if with_filter["max_drawdown_pct"] is not None and baseline["max_drawdown_pct"] is not None and with_filter["max_drawdown_pct"] > baseline["max_drawdown_pct"]:  # less negative = better
            improves.append("drawdown")
        if with_filter["recovery_factor"] is not None and baseline["recovery_factor"] is not None and with_filter["recovery_factor"] > baseline["recovery_factor"]:
            improves.append("recovery_factor")

    verdict = "ACCEPTED" if improves else "REJECTED"
    return {
        "strategy_id": strategy_id, "filter": name, "verdict": verdict, "improves": ",".join(improves),
        "baseline_n": baseline["n"], "baseline_expectancy_r": baseline["expectancy_r"], "baseline_pf": baseline["profit_factor"],
        "with_n": with_filter["n"], "with_expectancy_r": with_filter["expectancy_r"], "with_pf": with_filter["profit_factor"],
        "with_drawdown_pct": with_filter["max_drawdown_pct"], "with_recovery_factor": with_filter["recovery_factor"],
        "without_n": without_filter["n"], "without_expectancy_r": without_filter["expectancy_r"],
    }


def main() -> None:
    df = pd.read_parquet(OUT_DIR / "master_feature_dataset_with_itqs.parquet")
    rows = []

    for sid in ["S3", "S4"]:
        sub = df[df.strategy_id == sid]

        rows.append(evaluate_filter(df, sid, "OB Freshness = FRESH", df.ob_freshness_status == "FRESH"))
        rows.append(evaluate_filter(df, sid, "OB Quality > median", df.ob_quality_score > sub["ob_quality_score"].median()))
        rows.append(evaluate_filter(df, sid, "ITQS >= 55 (bucket A/B)", df.itqs >= 55))
        rows.append(evaluate_filter(df, sid, "Session = London", df.session == "london"))
        rows.append(evaluate_filter(df, sid, "Session = Sydney (Asian)", df.session == "sydney"))
        rows.append(evaluate_filter(df, sid, "Volatility = high", df.volatility_state == "high"))
        rows.append(evaluate_filter(df, sid, "Trend = ranging", df.trend_state == "ranging"))
        rows.append(evaluate_filter(df, sid, "Not a gap day", df.is_gap_day == False))  # noqa: E712

        atr_median = sub["atr_pips_at_entry"].median()
        rows.append(evaluate_filter(df, sid, "ATR above median (higher volatility)", df.atr_pips_at_entry > atr_median))
        rows.append(evaluate_filter(df, sid, "ATR below median (lower volatility)", df.atr_pips_at_entry <= atr_median))

        ob_age_median = sub["ob_age_candles"].median()
        rows.append(evaluate_filter(df, sid, "OB age below median (younger OB)", df.ob_age_candles <= ob_age_median))

        liq_touch_median = sub["liquidity_touches"].median()
        rows.append(evaluate_filter(df, sid, "Liquidity strength = strong", df.liquidity_strength == "strong"))
        rows.append(evaluate_filter(df, sid, f"Liquidity touches > median", df.liquidity_touches > liq_touch_median))

        pdh_median = sub["pdh_distance_pips"].median()
        rows.append(evaluate_filter(df, sid, "Near PDH/PDL (below median distance)", df.pdh_distance_pips <= pdh_median))

        rows.append(evaluate_filter(df, sid, "Confidence score above median", df.confidence_score > sub["confidence_score"].median()))
        rows.append(evaluate_filter(df, sid, "Entry hour 07:00-16:00 UTC (London+NY overlap window)", df.entry_hour_utc.between(7, 16)))

    out = pd.DataFrame(rows)
    save_feature_dataset(out, OUT_DIR / "alpha_filter_report.parquet", index=False)
    out.to_csv(OUT_DIR / "alpha_filter_report.csv", index=False)
    print(out[["strategy_id", "filter", "verdict", "improves", "baseline_expectancy_r", "with_expectancy_r", "with_n"]].to_string(index=False))
    print(f"\nWrote {OUT_DIR / 'alpha_filter_report.parquet'} and .csv")

    accepted = out[out.verdict == "ACCEPTED"]
    print(f"\n{len(accepted)}/{len(out)} filters ACCEPTED")


if __name__ == "__main__":
    main()
