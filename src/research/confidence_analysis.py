"""
Confidence Analysis: determines whether the Task 3 confidence score
actually correlates with profitability -- i.e. whether the deterministic,
rule-based confidence model has predictive value, or whether it's just
noise dressed up as a number.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.backtest.trade import TradeStatus
from src.research.analysis_utils import group_metrics


def _bucket(score: float) -> str:
    lower = int(score // 10) * 10
    return f"{lower}-{lower + 10}"


def analyze_confidence_buckets(trades: list, starting_balance: float = 10_000.0) -> pd.DataFrame:
    df = group_metrics(trades, lambda t: _bucket(t.confidence_score), starting_balance)
    df = df.rename(columns={"group": "confidence_range"})
    return df.sort_values("confidence_range").reset_index(drop=True)


def confidence_profitability_correlation(trades: list) -> dict:
    """Spearman rank correlation between confidence_score and both
    realized_pnl and r_multiple. A positive, non-trivial correlation
    supports the confidence model having predictive value; near-zero (or
    negative) correlation means it does not, at least on this dataset."""
    closed = [t for t in trades if t.status == TradeStatus.CLOSED.value]
    if len(closed) < 3:
        return {"n": len(closed), "spearman_confidence_vs_pnl": None, "spearman_confidence_vs_r": None}

    df = pd.DataFrame({
        "confidence": [t.confidence_score for t in closed],
        "pnl": [t.realized_pnl for t in closed],
        "r_multiple": [t.r_multiple if t.r_multiple is not None else np.nan for t in closed],
    })
    corr_pnl = df["confidence"].corr(df["pnl"], method="spearman")
    corr_r = df["confidence"].corr(df["r_multiple"], method="spearman")
    return {
        "n": len(closed),
        "spearman_confidence_vs_pnl": round(float(corr_pnl), 4) if pd.notna(corr_pnl) else None,
        "spearman_confidence_vs_r": round(float(corr_r), 4) if pd.notna(corr_r) else None,
    }
