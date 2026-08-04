"""
Task 10 Phase 2 — Institutional Opportunity Score (IOS).

An explainable, non-AI ranking score combining eight components, each
tied to a specific measured finding from Task 8/9 (never invented):

    ITQS                        -- Task 9 Phase 4 (freshness/quality/
                                    displacement/liquidity/confidence composite)
    Expected expectancy          -- this opportunity's own R:R times the
                                    strategy+symbol's HISTORICAL win rate
                                    (Task 8 per-symbol strategy_metrics)
    Order Block quality          -- repeated emphasis: Task 9's #1 finding
                                    (t-test p<0.01 both strategies)
    Order Block freshness        -- repeated emphasis: Task 9's strongest
                                    finding (chi2 p<0.001 both strategies,
                                    win rate 71-73% FRESH vs 45-46% MITIGATED)
    Market regime suitability    -- Task 9 Phase 7's preferred/avoid regimes
    Session suitability          -- Task 9 Phase 7/10's session findings
    Symbol historical strength   -- Task 9 Phase 8's star ratings
    Portfolio diversification    -- correlation with the CURRENT open/queued
                                    portfolio (lower correlation = higher score)

Weights are fixed constants set from the RELATIVE size of each Task 9
finding's measured effect -- OB freshness and quality repeat their Task 9
weight-of-evidence (the single largest, most significant effect found),
matching the same "no curve fitting" principle already used for ITQS.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
EDGE_DIR = ROOT / "reports" / "edge_refinement"

IOS_WEIGHTS = {
    "itqs": 0.25,
    "expected_expectancy": 0.20,
    "ob_quality": 0.15,
    "ob_freshness": 0.15,
    "regime_suitability": 0.10,
    "session_suitability": 0.05,
    "symbol_strength": 0.05,
    "portfolio_diversification": 0.05,
}

# Task 9 Phase 7 findings, hardcoded from the measured regime report
# (reports/edge_refinement/market_regime_report.csv) -- not re-derived at
# runtime so the "why" stays traceable to the exact numbers in that file.
PREFERRED_SESSIONS = {"S3": {"london"}, "S4": {"tokyo", "london"}}
AVOID_SESSIONS = {"S3": {"sydney"}, "S4": set()}
PREFERRED_VOLATILITY = {"S3": "high", "S4": "high"}
PREFERRED_TREND = {"S3": "ranging", "S4": None}  # S4 showed no clearly forbidden/preferred trend state
AVOID_GAP_DAY = {"S3": True, "S4": False}  # S3: -6.98 expectancy on gap days (Task 9 Phase 7); S4 had no reliable gap-day penalty


def _load_symbol_strength() -> dict:
    """{(strategy_id, symbol): composite score 0..1} from Task 9 Phase 8."""
    path = EDGE_DIR / "symbol_specialisation.csv"
    if not path.exists():
        return {}
    df = pd.read_csv(path)
    return {(row.strategy_id, row.symbol): row.composite for row in df.itertuples()}


def _load_historical_win_rate() -> dict:
    """{(strategy_id, symbol): win_rate} from Task 9 Phase 8 (same file,
    win_rate column) -- used for the "expected expectancy" component."""
    path = EDGE_DIR / "symbol_specialisation.csv"
    if not path.exists():
        return {}
    df = pd.read_csv(path)
    return {(row.strategy_id, row.symbol): row.win_rate for row in df.itertuples()}


_SYMBOL_STRENGTH = None
_WIN_RATE = None


def _lazy_load():
    global _SYMBOL_STRENGTH, _WIN_RATE
    if _SYMBOL_STRENGTH is None:
        _SYMBOL_STRENGTH = _load_symbol_strength()
    if _WIN_RATE is None:
        _WIN_RATE = _load_historical_win_rate()


def _regime_suitability(opp) -> float:
    sid = opp.strategy_id
    score = 0.5  # neutral default when no specific finding applies
    if opp.session in PREFERRED_SESSIONS.get(sid, set()):
        score = max(score, 0.85)
    if opp.session in AVOID_SESSIONS.get(sid, set()):
        score = min(score, 0.15)
    if opp.volatility_state == PREFERRED_VOLATILITY.get(sid):
        score = min(1.0, score + 0.15)
    if PREFERRED_TREND.get(sid) and opp.trend_state == PREFERRED_TREND[sid]:
        score = min(1.0, score + 0.1)
    if AVOID_GAP_DAY.get(sid) and opp.is_gap_day:
        score = min(score, 0.1)
    return round(max(0.0, min(1.0, score)), 4)


def _session_suitability(opp) -> float:
    sid = opp.strategy_id
    if opp.session in PREFERRED_SESSIONS.get(sid, set()):
        return 1.0
    if opp.session in AVOID_SESSIONS.get(sid, set()):
        return 0.0
    return 0.5


def _symbol_strength(opp) -> float:
    _lazy_load()
    return round(float(_SYMBOL_STRENGTH.get((opp.strategy_id, opp.symbol), 0.5)), 4)


def _expected_expectancy(opp) -> float:
    """opportunity's own R:R (already 0..N) scaled by the strategy+symbol's
    historical win rate, then min-max squashed to roughly 0-1 via a fixed
    reference ceiling (expected_r * win_rate of 3.0 -- a strong R:R at a
    50%+ win rate -- maps to 1.0). Ceiling is a fixed constant, not fit to
    this dataset."""
    _lazy_load()
    win_rate = _WIN_RATE.get((opp.strategy_id, opp.symbol), 0.45)
    raw = opp.expected_r * win_rate
    return round(min(1.0, raw / 1.5), 4)


def _ob_quality_component(opp) -> float:
    return round(min(max(float(opp.ob_quality), 0.0), 1.0), 4) if opp.ob_quality is not None else 0.5


def _ob_freshness_component(opp) -> float:
    if opp.ob_freshness == "FRESH":
        return 1.0
    if opp.ob_freshness == "MITIGATED":
        return 0.0
    return 0.5


def portfolio_diversification_component(opp, current_portfolio: list) -> float:
    """1.0 if nothing in the current portfolio shares this opportunity's
    strategy or either currency leg; degrades toward 0.0 as overlap
    increases. `current_portfolio`: list of already-approved/open
    Opportunity objects (Phase 3/4 populate this as they go)."""
    if not current_portfolio:
        return 1.0
    opp_legs = set(opp.currency_legs())
    penalties = 0
    for held in current_portfolio:
        if held.strategy_id == opp.strategy_id:
            penalties += 1
        if set(held.currency_legs()) & opp_legs:
            penalties += 1
        if held.symbol == opp.symbol:
            penalties += 1
    return round(max(0.0, 1.0 - 0.2 * penalties), 4)


def compute_ios(opp, current_portfolio: list | None = None) -> dict:
    """Returns {"ios": float 0-100, "components": {name: 0..1}} -- every
    component individually inspectable for Phase 7's explainability layer."""
    current_portfolio = current_portfolio or []
    components = {
        "itqs": round(opp.itqs / 100.0, 4),
        "expected_expectancy": _expected_expectancy(opp),
        "ob_quality": _ob_quality_component(opp),
        "ob_freshness": _ob_freshness_component(opp),
        "regime_suitability": _regime_suitability(opp),
        "session_suitability": _session_suitability(opp),
        "symbol_strength": _symbol_strength(opp),
        "portfolio_diversification": portfolio_diversification_component(opp, current_portfolio),
    }
    ios = 100.0 * sum(IOS_WEIGHTS[k] * v for k, v in components.items())
    return {"ios": round(ios, 2), "components": components}


def rank_opportunities(opportunities: list, current_portfolio: list | None = None) -> pd.DataFrame:
    rows = []
    for opp in opportunities:
        result = compute_ios(opp, current_portfolio)
        rows.append({
            "opportunity_id": opp.opportunity_id, "strategy_id": opp.strategy_id, "symbol": opp.symbol,
            "direction": opp.direction, "ios": result["ios"], **{f"component_{k}": v for k, v in result["components"].items()},
        })
    df = pd.DataFrame(rows)
    return df.sort_values("ios", ascending=False).reset_index(drop=True) if not df.empty else df
