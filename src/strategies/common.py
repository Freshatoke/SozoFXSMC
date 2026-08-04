"""
Common Strategy Engine infrastructure: the shared Signal schema, a
deterministic rule-based confidence scorer, and reason-code helpers used
identically by every strategy module (S1-S5). None of this detects market
structure or SMC features itself -- it only packages what a strategy
already found (via `src.strategies.context.MarketContext`) into the
uniform output schema required by the task brief.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import pandas as pd


@dataclass
class Signal:
    signal_id: str
    strategy_id: str
    timestamp: pd.Timestamp
    symbol: str
    timeframe: str
    direction: str                    # "bullish" or "bearish"
    entry_zone: tuple                 # (low, high)
    stop_loss_reference: float
    target_reference: float
    confidence_score: float
    reason_codes: list
    confluence_snapshot: dict
    market_structure_state: str
    session: Optional[str]
    risk_reference: dict
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["entry_zone"] = list(d["entry_zone"])
        return d


# ---------------------------------------------------------------------------
# Confidence scoring
#
# Deterministic, rule-based, fully explainable -- NOT machine-learned.
# Each strategy supplies a dict of {factor_name: (weight, value_0_to_1)}
# covering only the factors actually applicable to it (e.g. S1 has no
# "LiquiditySweep" factor). The score is a weighted average scaled to
# 0-100, normalized by the sum of weights actually supplied so that a
# strategy which doesn't evaluate a given factor is never penalized for
# it. Every contributing factor is returned alongside the score so a
# researcher can see exactly why a signal scored the way it did.
# ---------------------------------------------------------------------------

# Canonical weights for the universal contributors named in the task
# brief. Strategies may omit any of these (if not applicable) or add
# strategy-specific ones (e.g. "GapQuality" only applies to S1).
DEFAULT_FACTOR_WEIGHTS = {
    "FreshOrderBlock": 0.20,
    "CHoCHConfirmation": 0.20,
    "DisplacementQuality": 0.15,
    "LiquiditySweep": 0.15,
    "FVGAlignment": 0.10,
    "Engulfing": 0.05,
    "SessionContext": 0.05,
    "GapQuality": 0.15,
    "TrendAlignment": 0.10,
}


def compute_confidence(factor_values: dict, weights: dict = None) -> tuple:
    """factor_values: {factor_name: value in [0, 1]}. Only include factors
    this strategy actually evaluates. Returns (score_0_to_100, contributions)
    where contributions records weight/value/contribution per factor for
    full explainability."""
    weights = weights or DEFAULT_FACTOR_WEIGHTS
    contributions = {}
    weighted_sum = 0.0
    weight_total = 0.0
    for name, value in factor_values.items():
        w = weights.get(name, 0.1)  # unknown/strategy-specific factors default to a modest weight
        value = max(0.0, min(1.0, value))
        contribution = w * value
        contributions[name] = {"weight": w, "value": value, "contribution": contribution}
        weighted_sum += contribution
        weight_total += w

    score = round(100.0 * weighted_sum / weight_total, 2) if weight_total > 0 else 0.0
    return score, contributions


def build_reason_codes(strategy_id: str, condition_codes: list, confidence: float) -> list:
    """[strategy_id, *condition_codes, ConfidenceNN] -- matches the exact
    format shown in the task brief's worked examples."""
    return [strategy_id, *condition_codes, f"Confidence{int(round(confidence))}"]


def make_signal_id(strategy_id: str, symbol: str, timeframe: str, timestamp: pd.Timestamp, seq: int) -> str:
    return f"{strategy_id}_{symbol}_{timeframe}_{timestamp.strftime('%Y%m%dT%H%M%S')}_{seq}"


def dedupe_signals(signals: list) -> list:
    """Prevents duplicate signals for the same (strategy, symbol,
    timeframe, timestamp, direction) tuple -- a strategy's own logic
    should already avoid re-firing on the same setup, but this is a final
    safety net shared by every strategy and the runner."""
    seen = set()
    out = []
    for s in signals:
        key = (s.strategy_id, s.symbol, s.timeframe, s.timestamp, s.direction)
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out
