"""Task 9 Phase 8 — Symbol Specialisation for S3/S4.

Composite score = mean of min-max normalized (expectancy, profit_factor,
sharpe_ratio) WITHIN each strategy's own 7-symbol set (relative ranking,
same honest-scope approach as IES/Calmar elsewhere in this platform --
absolute cross-strategy comparability isn't meaningful on this sample
size). Star rating is a direct linear mapping of that composite score
into 1-5 stars, not a separate judgment call.
"""

from __future__ import annotations

import pickle
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CACHE_DIR = ROOT / "reports" / "institutional_research" / "_cache"
OUT_DIR = ROOT / "reports" / "edge_refinement"
SYMBOLS = ["AUDUSD", "EURUSD", "GBPUSD", "NZDUSD", "USDCAD", "USDCHF", "USDJPY"]


def _minmax(s: pd.Series) -> pd.Series:
    lo, hi = s.min(), s.max()
    if hi == lo:
        return pd.Series(0.5, index=s.index)
    return (s - lo) / (hi - lo)


def stars(composite: float) -> str:
    n = max(1, min(5, round(1 + composite * 4)))
    return "★" * n + "☆" * (5 - n)


def main() -> None:
    frames = []
    for symbol in SYMBOLS:
        with open(CACHE_DIR / f"{symbol}_tier1.pkl", "rb") as f:
            r = pickle.load(f)
        frames.append(r["strategy_metrics"])
    combined = pd.concat(frames, ignore_index=True)
    s34 = combined[combined.strategy_id.isin(["S3", "S4"])].copy()

    rows = []
    for sid, grp in s34.groupby("strategy_id"):
        grp = grp.copy()
        grp["n_expectancy"] = _minmax(grp["expectancy"])
        grp["n_pf"] = _minmax(grp["profit_factor"].clip(upper=3.0))
        grp["n_sharpe"] = _minmax(grp["sharpe_ratio"])
        grp["composite"] = (grp["n_expectancy"] + grp["n_pf"] + grp["n_sharpe"]) / 3.0
        grp["star_rating"] = grp["composite"].apply(stars)
        rows.append(grp)

    out = pd.concat(rows, ignore_index=True).sort_values(["strategy_id", "composite"], ascending=[True, False])
    out.to_csv(OUT_DIR / "symbol_specialisation.csv", index=False)
    print(out[["strategy_id", "symbol", "num_trades", "expectancy", "profit_factor", "sharpe_ratio", "composite", "star_rating"]].to_string(index=False))
    print(f"\nWrote {OUT_DIR / 'symbol_specialisation.csv'}")

    for sid in ["S3", "S4"]:
        sub = out[out.strategy_id == sid]
        best = sub.iloc[0]
        worst = sub.iloc[-1]
        print(f"{sid}: best={best['symbol']} ({best['star_rating']}), worst={worst['symbol']} ({worst['star_rating']})")


if __name__ == "__main__":
    main()
