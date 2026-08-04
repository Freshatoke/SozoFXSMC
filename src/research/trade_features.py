"""
Task 9 Phase 1 — Deep Trade Review: extracts every available platform
feature for a single closed trade into one flat, research-ready row.

Every feature here is derived from data the platform ALREADY computes
(Order Blocks, liquidity levels, displacement, FVGs, structure events,
market conditions, sessions, reference levels) -- nothing here changes
signal generation or backtest simulation. This is pure post-hoc feature
extraction over already-closed historical trades, the same "explains
outcomes, does not gate decisions" pattern already used by
`src.research.filter_analysis.fresh_ob_predicate`.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.backtest.trade import TradeStatus

PIP_SIZE_DEFAULT = 0.0001


def _pip_size(symbol: str) -> float:
    # Matches ExecutionConfig.pip_size convention used across the platform:
    # JPY pairs quote 2 decimal places (pip = 0.01), everything else 4 (0.0001).
    return 0.01 if symbol.endswith("JPY") else PIP_SIZE_DEFAULT


class FeatureContextIndex:
    """Precomputes O(1) id->record lookups for one MarketContext so
    extracting features for thousands of trades doesn't repeatedly
    filter full DataFrames. Built ONCE per symbol, reused across all of
    that symbol's trades."""

    def __init__(self, context, timeframe: str = "M15"):
        self.context = context
        self.timeframe = timeframe
        self.ob_by_id = {row["ob_id"]: row for row in context.order_blocks(timeframe).to_dict("records")}
        self.liq_by_id = {row["liquidity_id"]: row for row in context.liquidity(timeframe).to_dict("records")}
        self.fvg_by_id = {row["fvg_id"]: row for row in context.fvgs(timeframe).to_dict("records")} if not context.fvgs(timeframe).empty else {}

        m1 = context.m1
        self.ts_list = m1["timestamp"].tolist()
        self.close = m1["close"].to_numpy()
        self.high = m1["high"].to_numpy()
        self.low = m1["low"].to_numpy()
        # Rolling ATR(14) on M1, computed once -- same formula as
        # src.research.market_conditions._rolling_atr, reused here rather
        # than duplicated so both modules agree on what "ATR" means.
        prev_close = m1["close"].shift(1)
        tr = pd.concat([
            m1["high"] - m1["low"], (m1["high"] - prev_close).abs(), (m1["low"] - prev_close).abs(),
        ], axis=1).max(axis=1)
        self.atr14 = tr.rolling(14, min_periods=1).mean().to_numpy()

        ref = context.reference_levels
        self.pdh = ref[ref.level_type == "PDH"].sort_values("available_from") if not ref.empty else ref
        self.pdl = ref[ref.level_type == "PDL"].sort_values("available_from") if not ref.empty else ref

        sessions = context.sessions
        self.asian_sessions = sessions[sessions.session_name == "tokyo"].sort_values("start_utc") if not sessions.empty else sessions

    def atr_at(self, timestamp) -> float:
        idx = np.searchsorted(self.ts_list, timestamp, side="right") - 1
        if idx < 0:
            return 0.0
        return float(self.atr14[idx])

    def level_before(self, levels: pd.DataFrame, timestamp):
        if levels is None or levels.empty:
            return None
        idx = levels["available_from"].searchsorted(timestamp, side="right") - 1
        if idx < 0:
            return None
        return levels.iloc[idx]

    def asian_range_before(self, timestamp):
        if self.asian_sessions is None or self.asian_sessions.empty:
            return None
        idx = self.asian_sessions["end_utc"].searchsorted(timestamp, side="right") - 1
        if idx < 0:
            return None
        return self.asian_sessions.iloc[idx]


def extract_trade_features(trade, index: FeatureContextIndex) -> dict:
    """Returns one flat dict of features for a single trade. Only CLOSED
    trades produce a meaningful outcome label; callers should filter to
    `status == CLOSED` before using `is_winner`/`r_multiple` downstream,
    but every other feature is computed regardless of outcome so
    near-miss/breakeven/losing trades are equally represented (per this
    task's explicit "analyse winners, losers, near misses, breakeven"
    instruction)."""
    symbol = trade.symbol
    pip = _pip_size(symbol)
    snap = trade.confluence_snapshot or {}
    meta = trade.metadata or {}
    codes = trade.reason_codes or []
    entry_ts = trade.entry_timestamp
    entry_price = trade.entry_price

    ob = index.ob_by_id.get(snap.get("order_block_id"))
    liq = index.liq_by_id.get(snap.get("liquidity_id"))

    ob_age_candles = None
    ob_size_pips = None
    ob_quality = None
    if ob is not None and entry_ts is not None:
        ob_age_candles = int((entry_ts - ob["creation_timestamp"]).total_seconds() / 60) if ob["creation_timestamp"] is not None else None
        ob_size_pips = round((ob["high"] - ob["low"]) / pip, 2)
        ob_quality = ob.get("quality_score")

    liq_strength = liq.get("strength") if liq is not None else None
    liq_touches = liq.get("number_of_touches") if liq is not None else None

    choch_ts = snap.get("choch_timestamp")
    swept_ts = snap.get("swept_timestamp")
    choch_to_entry_candles = int((entry_ts - choch_ts).total_seconds() / 60) if (choch_ts is not None and entry_ts is not None) else None
    sweep_to_choch_candles = int((choch_ts - swept_ts).total_seconds() / 60) if (choch_ts is not None and swept_ts is not None) else None

    atr = index.atr_at(entry_ts) if entry_ts is not None else None
    atr_pips = round(atr / pip, 2) if atr else None

    pdh_row = index.level_before(index.pdh, entry_ts) if entry_ts is not None else None
    pdl_row = index.level_before(index.pdl, entry_ts) if entry_ts is not None else None
    pdh_distance_pips = round(abs(entry_price - pdh_row["value"]) / pip, 2) if (pdh_row is not None and entry_price is not None) else None
    pdl_distance_pips = round(abs(entry_price - pdl_row["value"]) / pip, 2) if (pdl_row is not None and entry_price is not None) else None

    asian = index.asian_range_before(entry_ts) if entry_ts is not None else None
    asian_range_pips = round((asian["high"] - asian["low"]) / pip, 2) if asian is not None else None
    asian_distance_pips = None
    if asian is not None and entry_price is not None:
        if entry_price > asian["high"]:
            asian_distance_pips = round((entry_price - asian["high"]) / pip, 2)
        elif entry_price < asian["low"]:
            asian_distance_pips = round((asian["low"] - entry_price) / pip, 2)
        else:
            asian_distance_pips = 0.0

    is_closed = trade.status == TradeStatus.CLOSED.value
    is_winner = bool(trade.realized_pnl > 0) if is_closed else None
    is_breakeven = bool(is_closed and abs(trade.realized_pnl) < 1e-6)
    r = trade.r_multiple
    is_near_miss = bool(is_closed and r is not None and -0.3 <= r < 0)  # closed slightly negative -- close to breakeven

    return {
        "trade_id": trade.trade_id, "symbol": symbol, "strategy_id": trade.strategy_id,
        "direction": trade.direction, "status": trade.status,
        "entry_timestamp": entry_ts, "entry_hour_utc": entry_ts.hour if entry_ts is not None else None,
        "entry_weekday": entry_ts.dayofweek if entry_ts is not None else None,

        # Outcome (only meaningful for CLOSED trades)
        "is_closed": is_closed, "is_winner": is_winner, "is_breakeven": is_breakeven, "is_near_miss": is_near_miss,
        "realized_pnl": trade.realized_pnl if is_closed else None,
        "r_multiple": r, "duration_candles": trade.duration_candles,
        "mae": trade.mae, "mfe": trade.mfe, "exit_reason": trade.exit_reason,

        # Confidence
        "confidence_score": trade.confidence_score,

        # Session / regime (already labelled onto trade.metadata by Task 8/5's
        # market_conditions.label_trades_with_conditions / session_analysis.label_trade_sessions)
        "session": trade.session, "active_sessions": ",".join(meta.get("active_sessions", []) or []),
        "session_overlap": meta.get("session_overlap"),
        "trend_state": meta.get("trend_state"), "volatility_state": meta.get("volatility_state"),
        "directional_bias": meta.get("directional_bias"),
        "is_gap_day": meta.get("is_gap_day"), "is_news_day": meta.get("is_news_day"),

        # CHoCH / BOS sequence quality
        "has_choch": "CHoCH" in " ".join(codes) or any("CHoCH" in c for c in codes),
        "choch_to_entry_candles": choch_to_entry_candles,
        "sweep_to_choch_candles": sweep_to_choch_candles,

        # Order Block
        "has_order_block": ob is not None,
        "ob_age_candles": ob_age_candles, "ob_size_pips": ob_size_pips, "ob_quality_score": ob_quality,
        "ob_wick_ratio": ob.get("wick_ratio") if ob is not None else None,
        "ob_freshness_status": ob.get("freshness_status") if ob is not None else None,

        # Liquidity
        "has_liquidity_ref": liq is not None,
        "liquidity_strength": liq_strength, "liquidity_touches": liq_touches,

        # FVG / Engulfing / Displacement (from reason_codes -- these ARE
        # part of the strategy's own decision, recorded as booleans here)
        "has_fvg_alignment": any("FVG" in c for c in codes),
        "has_displacement_confirmed": "DisplacementConfirmed" in codes,

        # Volatility / ATR
        "atr_pips_at_entry": atr_pips,

        # Reference levels
        "pdh_distance_pips": pdh_distance_pips, "pdl_distance_pips": pdl_distance_pips,
        "asian_range_pips": asian_range_pips, "asian_range_distance_pips": asian_distance_pips,

        "reason_codes": ",".join(codes),
    }


def build_master_feature_dataset(trades_by_symbol: dict, strategy_ids: tuple = ("S3", "S4")) -> pd.DataFrame:
    """trades_by_symbol: {symbol: {"context": MarketContext, "trades_by_strategy": {sid: [Trade,...]}}}
    (exactly the shape of Task 8's cached per-symbol Tier 1 results)."""
    rows = []
    for symbol, res in trades_by_symbol.items():
        index = FeatureContextIndex(res["context"], timeframe="M15")
        for sid in strategy_ids:
            for trade in res["trades_by_strategy"].get(sid, []):
                rows.append(extract_trade_features(trade, index))
    return pd.DataFrame(rows)
