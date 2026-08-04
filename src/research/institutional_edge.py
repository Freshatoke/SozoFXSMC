"""
Task 8 — Institutional Strategy Research & Edge Discovery.

This module adds exactly two things not already covered by
`src/research/`'s Task 5 primitives, both explicitly requested by the
Task 8 brief:

    1. Failure categorization: assigns ONE primary failure reason to
       every losing CLOSED trade, using only fields already present on
       the `Trade` record (reason_codes, confidence_score, exit_reason,
       duration_candles, and the post-hoc market-condition/session labels
       `market_conditions.label_trades_with_conditions` /
       `session_analysis.label_trade_sessions` attach to `trade.metadata`).
       This is deterministic, rule-based, and fully explainable -- no ML,
       consistent with the rest of the platform's design philosophy.

    2. Institutional Edge Score (IES): a research-only ranking metric
       (never used for trading decisions) combining expectancy, profit
       factor, robustness, consistency, drawdown, portfolio contribution,
       and correlation into one comparable number per strategy.

Both operate on the OUTPUT of Task 3-7 primitives (Trade lists, the
existing `src.research.*` analysis functions) -- nothing here recomputes
signals, re-simulates trades, or touches trading logic.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from src.backtest.trade import TradeStatus
from src.research.analysis_utils import group_metrics

# ---------------------------------------------------------------------------
# Failure categorization
# ---------------------------------------------------------------------------

# Priority order matters: a trade could technically match more than one
# category (e.g. low confidence AND stopped out fast) -- the first
# matching rule in this order is recorded as the PRIMARY reason. This
# ordering reflects "how early in the signal's life did the weakness
# appear": a structurally missing confluence (no fresh OB / no confirmed
# displacement) explains the trade before timing/regime factors do.
FAILURE_CATEGORIES = [
    "ExpiredOrMissingOrderBlock",
    "WeakDisplacement",
    "MissingConfluence",
    "WrongMarketRegime",
    "LateConfirmation",
    "LiquidityFailure",
    "PoorTiming",
    "Other",
]

_OB_REFERENCING_STRATEGIES = {"S2", "S3", "S4", "S5"}  # every strategy except S1 conditions entries on an Order Block


def compute_negative_regime_buckets(trades_by_strategy: dict, min_sample_size: int = 15) -> dict:
    """{strategy_id: set of (trend_state, volatility_state) tuples whose
    aggregate expectancy is negative for that strategy} -- computed from
    the data itself, not assumed. Used by `categorize_failure` to tag
    "wrong market regime" losses using evidence, not a fixed heuristic.

    `min_sample_size` guards against small-sample noise: with only a
    handful of trades in a regime bucket, its expectancy can be negative
    by chance alone, which would make "WrongMarketRegime" dominate the
    failure report on any dataset without deep history (a real concern
    here, since several symbols in this task only have ~6 months of data
    vs. EURUSD's 6.5 years). Buckets with fewer than `min_sample_size`
    trades are excluded regardless of their sign."""
    out = {}
    for strategy_id, trades in trades_by_strategy.items():
        df = group_metrics(
            trades,
            lambda t: (t.metadata.get("trend_state", "unknown"), t.metadata.get("volatility_state", "unknown")),
        )
        if df.empty:
            out[strategy_id] = set()
            continue
        reliable = df[df["num_trades"] >= min_sample_size]
        out[strategy_id] = set(reliable[reliable["expectancy"] < 0]["group"])
    return out


def categorize_failure(trade, negative_regimes: set | None = None) -> str:
    """Returns the single primary failure category for one losing CLOSED
    trade (call only on trades with realized_pnl <= 0). See module
    docstring for the full rule set and its rationale."""
    codes = trade.reason_codes or []
    joined = " ".join(codes)
    has_ob_code = any("OrderBlock" in c for c in codes)
    has_displacement = "DisplacementConfirmed" in codes
    regime_key = (trade.metadata.get("trend_state", "unknown"), trade.metadata.get("volatility_state", "unknown"))

    if trade.strategy_id in _OB_REFERENCING_STRATEGIES and not has_ob_code:
        return "ExpiredOrMissingOrderBlock"
    if trade.strategy_id == "S3" and not has_displacement:
        return "WeakDisplacement"
    if trade.confidence_score is not None and trade.confidence_score < 60:
        return "MissingConfluence"
    if negative_regimes and regime_key in negative_regimes:
        return "WrongMarketRegime"
    if trade.exit_reason == "MAX_DURATION":
        return "PoorTiming"
    if trade.exit_reason == "STOP_LOSS" and (trade.duration_candles or 0) <= 3:
        return "LateConfirmation"
    if "Swept" in joined and trade.exit_reason == "STOP_LOSS":
        return "LiquidityFailure"
    return "Other"


def failure_frequency_report(trades_by_strategy: dict, negative_regimes: dict | None = None) -> pd.DataFrame:
    """One row per (strategy_id, failure_category) with count and % of
    that strategy's losing trades. `negative_regimes`: optional output of
    `compute_negative_regime_buckets`, keyed by strategy_id."""
    negative_regimes = negative_regimes or {}
    rows = []
    for strategy_id, trades in trades_by_strategy.items():
        losers = [t for t in trades if t.status == TradeStatus.CLOSED.value and t.realized_pnl <= 0]
        if not losers:
            continue
        neg = negative_regimes.get(strategy_id, set())
        counts: dict[str, int] = {}
        for t in losers:
            cat = categorize_failure(t, neg)
            counts[cat] = counts.get(cat, 0) + 1
        for cat, n in counts.items():
            rows.append({
                "strategy_id": strategy_id, "failure_category": cat,
                "count": n, "pct_of_losses": round(100.0 * n / len(losers), 2),
                "total_losses": len(losers),
            })
    if not rows:
        return pd.DataFrame(columns=["strategy_id", "failure_category", "count", "pct_of_losses", "total_losses"])
    return pd.DataFrame(rows).sort_values(["strategy_id", "count"], ascending=[True, False]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Institutional Edge Score (IES)
# ---------------------------------------------------------------------------

# Component weights -- sum to 1.0. Documented rationale in
# docs/INSTITUTIONAL_RESEARCH_REPORT.md alongside the formula.
IES_WEIGHTS = {
    "expectancy": 0.20,
    "profit_factor": 0.15,
    "robustness": 0.20,
    "consistency": 0.15,
    "drawdown": 0.15,       # inverted: lower drawdown -> higher score
    "portfolio_contribution": 0.10,
    "correlation": 0.05,    # inverted: lower avg correlation -> higher score
}

# Caps used to bound outlier ratios before min-max normalizing, so one
# extreme value (e.g. profit_factor = 40 from three lucky trades) doesn't
# compress every other strategy's score toward zero.
_PROFIT_FACTOR_CAP = 3.0
_DRAWDOWN_CAP_PCT = 50.0


def _minmax(values: dict) -> dict:
    """Min-max normalize a {key: value} dict to [0, 1]. If every value is
    equal, returns 0.5 for all (no information to rank on)."""
    vals = list(values.values())
    lo, hi = min(vals), max(vals)
    if hi == lo:
        return {k: 0.5 for k in values}
    return {k: (v - lo) / (hi - lo) for k, v in values.items()}


@dataclass
class IESInputs:
    strategy_id: str
    r_multiple_mean: float            # average R-multiple (instrument-agnostic expectancy proxy)
    profit_factor: float
    max_drawdown_pct: float           # signed or unsigned; abs() is taken internally
    positive_window_pct: float        # from walkforward_research.summarize_stability, 0..1
    expectancy_std_across_windows: float
    expectancy_mean_across_windows: float
    portfolio_diversification_benefit: float  # this strategy's best diversification_benefit when combined with others
    avg_abs_correlation_with_others: float    # mean |correlation| vs every other strategy


def compute_institutional_edge_scores(inputs: list) -> pd.DataFrame:
    """inputs: list of IESInputs, one per strategy being ranked (the
    normalization is RELATIVE to this set, exactly like this codebase's
    existing Calmar ratio -- see strategy_analysis.py's documented
    rationale for why relative ranking is the honest approach on a
    dataset too short for a universally-comparable absolute scale).

    Returns one row per strategy: every component score (0-1) plus the
    final IES (0-100), sorted descending by IES.
    """
    if not inputs:
        return pd.DataFrame()

    r_mean = {i.strategy_id: i.r_multiple_mean for i in inputs}
    pf = {i.strategy_id: min(i.profit_factor, _PROFIT_FACTOR_CAP) for i in inputs}
    dd = {i.strategy_id: min(abs(i.max_drawdown_pct), _DRAWDOWN_CAP_PCT) for i in inputs}
    robustness = {i.strategy_id: i.positive_window_pct for i in inputs}
    # consistency: coefficient-of-variation-based -- lower relative
    # dispersion of expectancy across rolling windows = more consistent.
    # Bounded to [0, 1] via 1 / (1 + CV) so a strategy with zero variance
    # scores 1.0 and increasingly noisy strategies asymptote toward 0.
    consistency = {}
    for i in inputs:
        mean_abs = abs(i.expectancy_mean_across_windows)
        cv = (i.expectancy_std_across_windows / mean_abs) if mean_abs > 1e-9 else (0.0 if i.expectancy_std_across_windows == 0 else 10.0)
        consistency[i.strategy_id] = 1.0 / (1.0 + cv)
    portfolio_contrib = {i.strategy_id: i.portfolio_diversification_benefit for i in inputs}
    correlation = {i.strategy_id: i.avg_abs_correlation_with_others for i in inputs}

    n_r = _minmax(r_mean)
    n_pf = {k: v / _PROFIT_FACTOR_CAP for k, v in pf.items()}
    n_dd = {k: v / _DRAWDOWN_CAP_PCT for k, v in dd.items()}     # 0 = no drawdown, 1 = at/above cap
    n_robust = robustness                                         # already 0..1
    n_consistency = consistency                                   # already 0..1
    n_portfolio = _minmax(portfolio_contrib)
    n_corr = _minmax(correlation)                                 # 0 = lowest correlation in the set

    rows = []
    for i in inputs:
        sid = i.strategy_id
        components = {
            "expectancy": n_r[sid],
            "profit_factor": n_pf[sid],
            "robustness": n_robust[sid],
            "consistency": n_consistency[sid],
            "drawdown": 1.0 - n_dd[sid],
            "portfolio_contribution": n_portfolio[sid],
            "correlation": 1.0 - n_corr[sid],
        }
        ies = 100.0 * sum(IES_WEIGHTS[k] * v for k, v in components.items())
        rows.append({
            "strategy_id": sid,
            "institutional_edge_score": round(ies, 2),
            **{f"component_{k}": round(v, 4) for k, v in components.items()},
            "raw_r_multiple_mean": i.r_multiple_mean,
            "raw_profit_factor": i.profit_factor,
            "raw_max_drawdown_pct": i.max_drawdown_pct,
            "raw_positive_window_pct": i.positive_window_pct,
            "raw_portfolio_diversification_benefit": i.portfolio_diversification_benefit,
            "raw_avg_abs_correlation": i.avg_abs_correlation_with_others,
        })
    return pd.DataFrame(rows).sort_values("institutional_edge_score", ascending=False).reset_index(drop=True)


def avg_abs_correlation(correlation_matrix: pd.DataFrame, strategy_id: str) -> float:
    """Mean |correlation| between `strategy_id` and every OTHER strategy
    in a `strategy_correlation`-shaped square DataFrame (index/columns =
    strategy ids)."""
    if strategy_id not in correlation_matrix.index:
        return 0.0
    row = correlation_matrix.loc[strategy_id].drop(labels=[strategy_id], errors="ignore")
    row = row.dropna()
    if row.empty:
        return 0.0
    return float(row.abs().mean())


def best_diversification_benefit(portfolio_analysis: pd.DataFrame, strategy_id: str) -> float:
    """The best (max) diversification_benefit across every portfolio
    combination that INCLUDES this strategy alongside at least one other
    -- i.e. "how much did this strategy help when added to a team",
    rather than its solo-portfolio row (which is always 0 benefit by
    definition, size == 1)."""
    if portfolio_analysis.empty:
        return 0.0
    mask = portfolio_analysis["combination"].apply(lambda c: strategy_id in c.split("+")) & (portfolio_analysis["num_strategies"] > 1)
    subset = portfolio_analysis[mask]
    if subset.empty:
        return 0.0
    return float(subset["diversification_benefit"].max())
