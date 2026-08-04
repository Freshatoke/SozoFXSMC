"""
Task 10 Phase 1 — Unified Opportunity Queue.

Collects every signal from every enabled strategy, across every symbol
with an available MarketContext, into one flat `Opportunity` record.
Reuses `src.research.trade_features.FeatureContextIndex` (Task 9) for
O(1) Order Block / liquidity / ATR / reference-level lookups rather than
duplicating that indexing logic -- an Opportunity is extracted the same
way a Trade's features were, just from a not-yet-executed `Signal`
instead of an already-closed `Trade`.

Nothing here generates signals or simulates execution -- it only
packages what `run_strategies` already produced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from src.research.trade_features import FeatureContextIndex, _pip_size
from src.research.itqs import compute_itqs_row


@dataclass
class Opportunity:
    opportunity_id: str
    strategy_id: str
    symbol: str
    direction: str
    entry: float
    stop: float
    target: float
    expected_r: float                 # |target - entry| / |entry - stop|
    itqs: float
    ob_quality: Optional[float]
    ob_freshness: Optional[str]       # "FRESH" | "MITIGATED" | None (no OB referenced)
    atr_pips: Optional[float]
    stop_distance_pips: float         # a measured market fact, not an allocation decision (that's Phase 3)
    session: Optional[str]
    trend_state: Optional[str]
    volatility_state: Optional[str]
    directional_bias: Optional[str]
    is_gap_day: Optional[bool]
    timestamp: pd.Timestamp
    confidence_score: float
    reason_codes: list = field(default_factory=list)
    confluence_snapshot: dict = field(default_factory=dict)

    def currency_legs(self) -> tuple:
        """('EUR', 'USD') for EURUSD, etc. -- used by Phase 3's per-currency
        exposure limit and Phase 5's currency-concentration check."""
        s = self.symbol.upper()
        return (s[:3], s[3:6]) if len(s) >= 6 else (s, "")


def _signal_to_opportunity(signal, index: FeatureContextIndex) -> Opportunity:
    symbol = signal.symbol
    pip = _pip_size(symbol)
    snap = signal.confluence_snapshot or {}
    low, high = signal.entry_zone
    entry = (low + high) / 2.0
    stop = signal.stop_loss_reference
    target = signal.target_reference
    stop_distance = abs(entry - stop)
    expected_r = round(abs(target - entry) / stop_distance, 4) if stop_distance > 0 else 0.0

    ob = index.ob_by_id.get(snap.get("order_block_id"))
    ob_quality = ob.get("quality_score") if ob is not None else None
    ob_freshness = ob.get("freshness_status") if ob is not None else None

    ts = signal.timestamp
    atr = index.atr_at(ts) if ts is not None else None
    atr_pips = round(atr / pip, 2) if atr else None

    # Reuse the exact ITQS formula from Task 9 (src.research.itqs) by
    # building the same field shape it expects -- a "row" with the
    # relevant keys. This is the SAME score, computed pre-trade instead
    # of post-hoc, which is the whole point of Task 10: it exists so it
    # can rank opportunities BEFORE they become trades.
    has_displacement = "DisplacementConfirmed" in (signal.reason_codes or [])
    liq = index.liq_by_id.get(snap.get("liquidity_id"))
    liquidity_strength = liq.get("strength") if liq is not None else None
    itqs_row = pd.Series({
        "ob_freshness_status": ob_freshness, "ob_quality_score": ob_quality,
        "has_displacement_confirmed": has_displacement, "liquidity_strength": liquidity_strength,
        "confidence_score": signal.confidence_score,
    })
    itqs = compute_itqs_row(itqs_row)

    return Opportunity(
        opportunity_id=signal.signal_id,
        strategy_id=signal.strategy_id, symbol=symbol, direction=signal.direction,
        entry=entry, stop=stop, target=target, expected_r=expected_r,
        itqs=itqs, ob_quality=ob_quality, ob_freshness=ob_freshness,
        atr_pips=atr_pips, stop_distance_pips=round(stop_distance / pip, 2),
        session=signal.session,
        trend_state=None, volatility_state=None, directional_bias=None, is_gap_day=None,  # filled by label_opportunities()
        timestamp=ts, confidence_score=signal.confidence_score,
        reason_codes=list(signal.reason_codes or []), confluence_snapshot=dict(snap),
    )


def trade_to_opportunity(trade, index: FeatureContextIndex) -> Optional[Opportunity]:
    """Task 10 Phase 8 (Paper Trading) helper: reconstructs the Opportunity
    that would have existed at signal time from an ALREADY-CLOSED historical
    `Trade` (Task 8/9's cached backtest results). Every field this builds
    from is a fact recorded BEFORE/AT entry (confluence_snapshot, reason_codes,
    confidence_score, entry_price, initial_stop_loss, take_profit_levels,
    entry_timestamp) -- never from the trade's outcome (realized_pnl,
    r_multiple, exit_reason are deliberately not read here), so replaying
    the decision engine over these reconstructed opportunities carries no
    look-ahead: the engine sees exactly what it would have seen live, and
    only Phase 8's SEPARATE outcome lookup (after a decision is made) uses
    the trade's actual result."""
    if trade.entry_price is None or trade.entry_timestamp is None or not trade.take_profit_levels:
        return None
    symbol = trade.symbol
    pip = _pip_size(symbol)
    snap = trade.confluence_snapshot or {}
    entry = trade.entry_price
    stop = trade.initial_stop_loss
    target = trade.take_profit_levels[0][0]
    stop_distance = abs(entry - stop) if stop is not None else 0.0
    expected_r = round(abs(target - entry) / stop_distance, 4) if stop_distance > 0 else 0.0

    ob = index.ob_by_id.get(snap.get("order_block_id"))
    ob_quality = ob.get("quality_score") if ob is not None else None
    ob_freshness = ob.get("freshness_status") if ob is not None else None

    ts = trade.entry_timestamp
    atr = index.atr_at(ts) if ts is not None else None
    atr_pips = round(atr / pip, 2) if atr else None

    has_displacement = "DisplacementConfirmed" in (trade.reason_codes or [])
    liq = index.liq_by_id.get(snap.get("liquidity_id"))
    liquidity_strength = liq.get("strength") if liq is not None else None
    itqs_row = pd.Series({
        "ob_freshness_status": ob_freshness, "ob_quality_score": ob_quality,
        "has_displacement_confirmed": has_displacement, "liquidity_strength": liquidity_strength,
        "confidence_score": trade.confidence_score,
    })
    itqs = compute_itqs_row(itqs_row)

    meta = trade.metadata or {}
    return Opportunity(
        opportunity_id=trade.trade_id,
        strategy_id=trade.strategy_id, symbol=symbol, direction=trade.direction,
        entry=entry, stop=stop, target=target, expected_r=expected_r,
        itqs=itqs, ob_quality=ob_quality, ob_freshness=ob_freshness,
        atr_pips=atr_pips, stop_distance_pips=round(stop_distance / pip, 2),
        session=trade.session,
        trend_state=meta.get("trend_state"), volatility_state=meta.get("volatility_state"),
        directional_bias=meta.get("directional_bias"), is_gap_day=meta.get("is_gap_day"),
        timestamp=ts, confidence_score=trade.confidence_score,
        reason_codes=list(trade.reason_codes or []), confluence_snapshot=dict(snap),
    )


def label_opportunities(opportunities: list, conditions: pd.DataFrame) -> None:
    """Attaches trend/volatility/directional-bias/gap-day regime labels
    (from `src.research.market_conditions.classify_market_conditions`)
    to each opportunity's timestamp, in place -- same as-of lookup Task 8
    used for closed trades, applied here to not-yet-executed signals."""
    if not opportunities or conditions.empty:
        return
    lookup = conditions.sort_values("timestamp")
    ts_list = lookup["timestamp"].tolist()
    for opp in opportunities:
        if opp.timestamp is None:
            continue
        idx = pd.Series(ts_list).searchsorted(opp.timestamp, side="right") - 1
        if idx < 0:
            continue
        row = lookup.iloc[idx]
        opp.trend_state = row["trend_state"]
        opp.volatility_state = row["volatility_state"]
        opp.directional_bias = row["directional_bias"]
        opp.is_gap_day = bool(row["is_gap_day"])


def build_opportunity_queue(signals: list, context, index: Optional[FeatureContextIndex] = None) -> list:
    """signals: list of Signal objects (from `run_strategies`, any subset
    of enabled strategies -- the caller decides what's "enabled").
    context: the MarketContext they were generated from (needed to build
    the FeatureContextIndex if one wasn't already built and cached)."""
    index = index or FeatureContextIndex(context, timeframe="M15")
    return [_signal_to_opportunity(s, index) for s in signals]


def opportunities_to_dataframe(opportunities: list) -> pd.DataFrame:
    rows = []
    for o in opportunities:
        rows.append({
            "opportunity_id": o.opportunity_id, "strategy_id": o.strategy_id, "symbol": o.symbol,
            "direction": o.direction, "entry": o.entry, "stop": o.stop, "target": o.target,
            "expected_r": o.expected_r, "itqs": o.itqs, "ob_quality": o.ob_quality, "ob_freshness": o.ob_freshness,
            "atr_pips": o.atr_pips, "stop_distance_pips": o.stop_distance_pips, "session": o.session,
            "trend_state": o.trend_state, "volatility_state": o.volatility_state,
            "directional_bias": o.directional_bias, "is_gap_day": o.is_gap_day,
            "timestamp": o.timestamp, "confidence_score": o.confidence_score,
        })
    return pd.DataFrame(rows)
