"""
Task 8 — builds institutional_research_report.pdf from the cached
research results, using matplotlib (same approach as
src.research.validation_campaign._write_pdf) since the plotly dashboard
already covers interactive exploration (research_dashboard.html).
"""

from __future__ import annotations

import pickle
import sys
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.backtest.metrics import build_equity_curve

CACHE_DIR = ROOT / "reports" / "institutional_research" / "_cache"
OUT_DIR = ROOT / "reports" / "institutional_research"
STRATEGY_IDS = ["S1", "S2", "S3", "S4", "S5"]
STARTING_BALANCE = 10_000.0


def load_all():
    per_symbol = {}
    for path in sorted(CACHE_DIR.glob("*_tier1.pkl")):
        symbol = path.name.replace("_tier1.pkl", "")
        with open(path, "rb") as f:
            per_symbol[symbol] = pickle.load(f)
    with open(CACHE_DIR / "EURUSD_tier2.pkl", "rb") as f:
        tier2 = pickle.load(f)
    with open(CACHE_DIR / "aggregate.pkl", "rb") as f:
        agg = pickle.load(f)
    return per_symbol, tier2, agg


def main() -> None:
    per_symbol, tier2, agg = load_all()
    ies = agg["ies_table"]
    portfolio = agg["portfolio_combined"]
    correlation = agg["correlation_combined"]

    pdf_path = OUT_DIR / "institutional_research_report.pdf"
    with PdfPages(pdf_path) as pdf:
        # Page 1: title + executive summary
        fig = plt.figure(figsize=(8.27, 11.69))
        fig.text(0.08, 0.94, "Institutional Strategy Research & Edge Discovery", fontsize=18, weight="bold")
        fig.text(0.08, 0.905, "Forex SMC Quant Platform -- Task 8", fontsize=11, color="gray")
        fig.text(0.08, 0.87, f"Symbols analysed: {', '.join(sorted(per_symbol.keys()))}", fontsize=10)
        fig.text(0.08, 0.85, "XAUUSD excluded from scope per explicit instruction.", fontsize=9, style="italic")
        fig.text(0.08, 0.81, "Executive Summary", fontsize=13, weight="bold")
        top = ies.iloc[0]
        bottom = ies.iloc[-1]
        summary_lines = [
            f"Top-ranked strategy by Institutional Edge Score: {top['strategy_id']} ({top['institutional_edge_score']:.1f})",
            f"Weakest strategy: {bottom['strategy_id']} ({bottom['institutional_edge_score']:.1f}) -- recommended for retirement",
            "S3 and S4 are positive-expectancy on every one of the 7 tested symbols.",
            "A critical RiskTracker consecutive-loss lockout bug was found and worked around (see full report).",
            "Portfolio diversification benefit is negative for most combinations despite low correlation --",
            "  the strongest portfolios pair two independently-strong strategies (S3+S4), not merely uncorrelated ones.",
            "Confidence score shows no measurable correlation with profitability on this dataset (Spearman ~0.004).",
        ]
        y = 0.77
        for line in summary_lines:
            fig.text(0.08, y, line, fontsize=9.5)
            y -= 0.03
        fig.text(0.08, 0.05, "See docs/INSTITUTIONAL_RESEARCH_REPORT.md for the complete 12-section report.", fontsize=8, color="gray")
        pdf.savefig(fig)
        plt.close(fig)

        # Page 2: IES ranking bar chart + strategy metrics table
        fig, axes = plt.subplots(2, 1, figsize=(8.27, 11.69))
        ax = axes[0]
        colors = ["#2ca02c" if v >= 60 else ("#ff7f0e" if v >= 45 else "#d62728") for v in ies["institutional_edge_score"]]
        ax.bar(ies["strategy_id"], ies["institutional_edge_score"], color=colors)
        ax.set_title("Institutional Edge Score Ranking")
        ax.set_ylabel("IES")
        for i, v in enumerate(ies["institutional_edge_score"]):
            ax.text(i, v + 1, f"{v:.1f}", ha="center", fontsize=9)

        ax2 = axes[1]
        ax2.axis("off")
        table_data = ies[["strategy_id", "institutional_edge_score", "raw_r_multiple_mean", "raw_profit_factor",
                           "raw_max_drawdown_pct", "raw_positive_window_pct"]].round(3).values.tolist()
        col_labels = ["Strategy", "IES", "Avg R", "Profit Factor", "Max DD %", "12mo Positive Windows %"]
        tbl = ax2.table(cellText=table_data, colLabels=col_labels, loc="center", cellLoc="center")
        tbl.auto_set_font_size(False)
        tbl.set_fontsize(9)
        tbl.scale(1, 1.8)
        ax2.set_title("Strategy Comparison (combined across symbols)", pad=20)
        pdf.savefig(fig)
        plt.close(fig)

        # Page 3: Equity curves (EURUSD, full history)
        if "EURUSD" in per_symbol:
            fig, ax = plt.subplots(figsize=(8.27, 11.69 / 2))
            for sid, trades in per_symbol["EURUSD"]["trades_by_strategy"].items():
                curve = build_equity_curve(trades, STARTING_BALANCE)
                if not curve.empty:
                    ax.plot(curve["timestamp"].dt.tz_localize(None), curve["balance"], label=sid)
            ax.set_title("Equity Curves by Strategy (EURUSD, full history)")
            ax.legend()
            ax.set_ylabel("Account Balance ($)")
            pdf.savefig(fig)
            plt.close(fig)

        # Page 4: Top portfolios + correlation heatmap
        fig, axes = plt.subplots(2, 1, figsize=(8.27, 11.69))
        ax = axes[0]
        top10 = portfolio.sort_values("expectancy", ascending=False).head(10)
        ax.barh(top10["combination"][::-1], top10["expectancy"][::-1], color="#1f77b4")
        ax.set_title("Top 10 Portfolio Combinations by Expectancy")
        ax.set_xlabel("Expectancy ($/trade)")

        ax2 = axes[1]
        im = ax2.imshow(correlation.values, cmap="RdBu_r", vmin=-1, vmax=1)
        ax2.set_xticks(range(len(correlation.columns)))
        ax2.set_xticklabels(correlation.columns)
        ax2.set_yticks(range(len(correlation.index)))
        ax2.set_yticklabels(correlation.index)
        ax2.set_title("Strategy Correlation Matrix")
        for i in range(len(correlation.index)):
            for j in range(len(correlation.columns)):
                ax2.text(j, i, f"{correlation.values[i, j]:.2f}", ha="center", va="center", fontsize=8)
        fig.colorbar(im, ax=ax2, fraction=0.046)
        pdf.savefig(fig)
        plt.close(fig)

        # Page 5: rolling window stability
        if tier2 is not None:
            fig, ax = plt.subplots(figsize=(8.27, 11.69 / 2))
            for sid in STRATEGY_IDS:
                df12 = tier2["window_detail"].get((sid, "12mo"))
                if df12 is not None and not df12.empty:
                    ax.plot(df12["test_start"].dt.tz_localize(None), df12["expectancy"], marker="o", label=sid)
            ax.axhline(0, color="gray", linestyle="--")
            ax.set_title("Rolling 12-Month Expectancy Stability (EURUSD)")
            ax.legend()
            pdf.savefig(fig)
            plt.close(fig)

    print(f"Wrote {pdf_path}")


if __name__ == "__main__":
    main()
