"""
Task 12.4 -- Causal (point-in-time) liquidity reference.

RESEARCH-ONLY. Does not modify src.features.liquidity (the existing batch
detector, kept as-is and documented as NOT causally valid for live use --
see docs/TASK_12_4_LIQUIDITY_PERFORMANCE_REPORT.md), nor
src.engine.incremental.IncrementalLiquidityTracker, nor any strategy.

WHY THIS EXISTS
----------------
`src.features.liquidity.detect_liquidity_levels` clusters ALL swings
within the `as_of_index` window in one global, PRICE-SORTED pass
(`_cluster_equal_levels`), then assigns the cluster's price as the MEAN
of every member. This is provably look-ahead for live/causal use: a
cluster's price (and therefore whether/when it gets swept) can change
retroactively once a LATER-confirmed swing merges into it, even for
queries about a point in time BEFORE that later swing existed. Proven in
this task with a synthetic case: a level's price shifted from 1.200 to
1.195, and its reported state flipped from ACTIVE to SWEPT, purely
because a later, time-independent swing joined its price cluster.

This module builds a genuinely causal reference: it processes confirmed
swings in TIME order (by `confirmed_timestamp`, the same field
src.structure.swings.detect_swings and src.structure.market_structure
already use as the causal "this is knowable as of" boundary) and a new
swing may only merge into an EXISTING cluster if that cluster is still
ACTIVE (not yet swept) at the moment the new swing is confirmed -- once
a level is swept, it is causally "used" and a later swing near the same
price starts a NEW, separate level. This is deliberately equivalent, by
construction, to `IncrementalLiquidityTracker.ingest_swing()`'s rule
(Task 12.3/12.4 finding: the incremental tracker's behavior is the
causally correct one, not a bug) -- implemented independently here
(from the same precomputed swings dataframe and candle arrays the batch
detector uses, not by calling the incremental tracker) so it serves as
a genuine independent cross-check, not a tautology.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from config.settings import LiquidityConfig, DEFAULT_LIQUIDITY_CONFIG, SwingConfig
from src.structure.swings import detect_swings


def detect_causal_liquidity_levels(
    df: pd.DataFrame,
    symbol: str,
    timeframe: str,
    config: LiquidityConfig = DEFAULT_LIQUIDITY_CONFIG,
    timestamp_col: str = "timestamp",
    as_of_index: int | None = None,
) -> pd.DataFrame:
    """Causal equivalent of src.features.liquidity.detect_liquidity_levels.

    Differs from it in exactly one way: swings are merged into clusters
    in TIME order (by confirmed_timestamp), and a swing may only merge
    into a cluster that has not yet been swept as of that swing's own
    confirmation time. A cluster's price is the running mean of only the
    members merged into it before it was (if ever) swept -- it never
    changes once the cluster is swept, and a later swing near the same
    price starts a brand-new cluster instead of reopening it.
    """
    n = len(df)
    last_index = (n - 1) if as_of_index is None else min(as_of_index, n - 1)
    ts = df[timestamp_col].reset_index(drop=True)
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    close = df["close"].to_numpy()

    swing_cfg = SwingConfig(left=config.swing_left, right=config.swing_right)
    swings = detect_swings(df, config=swing_cfg, timeframe_label=timeframe)
    swings = swings[swings["confirmed_timestamp"] <= ts.iloc[last_index]].reset_index(drop=True)
    swings = swings.sort_values(["confirmed_timestamp", "candle_index"]).reset_index(drop=True)

    tolerance = config.equal_level_tolerance
    records: list[dict] = []
    liq_seq = 0
    running_high_extreme = None
    running_low_extreme = None

    # side -> list of open (not-yet-swept, not-yet-finalized) cluster dicts,
    # each: {"members": [...], "price": float, "creation_timestamp": Timestamp,
    #        "creation_candle_index": int, "record_idx": int (index into records)}
    open_clusters: dict[str, list[dict]] = {"high": [], "low": []}

    def finalize_sweep_and_maybe_close(cluster: dict, side: str) -> None:
        """Walks forward from the cluster's creation to find first touch/
        sweep using ONLY information already known (candles up to
        last_index) -- identical sweep semantics to the batch detector,
        just scoped to one already-causally-valid cluster."""
        rec = records[cluster["record_idx"]]
        walk_start = cluster["creation_candle_index"] + 1
        if walk_start > last_index:
            return
        price = rec["price"]
        w_high = high[walk_start:last_index + 1]
        w_low = low[walk_start:last_index + 1]
        w_close = close[walk_start:last_index + 1]

        if side == "buy_side":
            touch_mask = w_high >= price
            sweep_mask = (w_high > price) & (w_close < price)
        else:
            touch_mask = w_low <= price
            sweep_mask = (w_low < price) & (w_close > price)

        if touch_mask.any() and rec["first_touch_timestamp"] is None:
            first_touch_idx = int(np.argmax(touch_mask))
            rec["first_touch_timestamp"] = ts.iloc[walk_start + first_touch_idx]

        if sweep_mask.any():
            sweep_idx = int(np.argmax(sweep_mask))
            rec["swept_timestamp"] = ts.iloc[walk_start + sweep_idx]
            rec["state"] = "SWEPT"
            if (last_index - (walk_start + sweep_idx)) >= config.archive_after_candles:
                rec["state"] = "ARCHIVED"

    for _, srow in swings.iterrows():
        swing_type = srow["swing_type"]
        side = "buy_side" if swing_type == "high" else "sell_side"
        price = float(srow["price"])
        candle_idx = int(srow["candle_index"])
        conf_ts = srow["confirmed_timestamp"]

        if swing_type == "high":
            running_high_extreme = price if running_high_extreme is None else max(running_high_extreme, price)
        else:
            running_low_extreme = price if running_low_extreme is None else min(running_low_extreme, price)

        # First, resolve sweep state for every currently-open cluster of
        # this side as of THIS swing's confirmation time (a cluster only
        # accepts a merge if it is still ACTIVE at that instant).
        still_open = []
        for cluster in open_clusters[swing_type]:
            finalize_sweep_and_maybe_close_asof(cluster, side, conf_ts, ts, high, low, close, records)
            if records[cluster["record_idx"]]["state"] == "ACTIVE":
                still_open.append(cluster)
        open_clusters[swing_type] = still_open

        merged = None
        for cluster in open_clusters[swing_type]:
            group_avg = records[cluster["record_idx"]]["price"]
            if abs(price - group_avg) <= tolerance * group_avg:
                merged = cluster
                break

        extreme = running_high_extreme if swing_type == "high" else running_low_extreme

        if merged is None:
            liq_seq += 1
            rec = {
                "liquidity_id": f"{symbol}_{timeframe}_LIQ_{liq_seq}",
                "type": "swing_high" if swing_type == "high" else "swing_low",
                "side": side,
                "scope": "external" if np.isclose(price, extreme, rtol=1e-9) else "internal",
                "price": price,
                "number_of_touches": 1,
                "strength": "weak",
                "creation_timestamp": conf_ts,
                "creation_candle_index": candle_idx,
                "first_touch_timestamp": None,
                "swept_timestamp": None,
                "state": "ACTIVE",
            }
            records.append(rec)
            cluster = {
                "members": [srow], "creation_candle_index": candle_idx,
                "record_idx": len(records) - 1,
            }
            open_clusters[swing_type].append(cluster)
        else:
            rec = records[merged["record_idx"]]
            m = len(merged["members"])
            rec["price"] = (rec["price"] * m + price) / (m + 1)
            merged["members"].append(srow)
            rec["number_of_touches"] = m + 1
            rec["type"] = "equal_high" if swing_type == "high" else "equal_low"
            rec["strength"] = "strong" if rec["number_of_touches"] >= config.min_touches_for_strength else "weak"
            rec["scope"] = "external" if np.isclose(rec["price"], extreme, rtol=1e-9) else rec["scope"]
            merged["creation_candle_index"] = min(merged["creation_candle_index"], candle_idx)

    # Final sweep resolution as of last_index for every still-open cluster.
    for side_key, side_name in (("high", "buy_side"), ("low", "sell_side")):
        for cluster in open_clusters[side_key]:
            finalize_sweep_and_maybe_close(cluster, side_name)

    columns = [
        "liquidity_id", "type", "side", "scope", "price", "number_of_touches",
        "strength", "creation_timestamp", "creation_candle_index",
        "first_touch_timestamp", "swept_timestamp", "state",
    ]
    out = pd.DataFrame.from_records(records, columns=columns)
    if not out.empty:
        out = out.sort_values("creation_timestamp").reset_index(drop=True)
    return out


def finalize_sweep_and_maybe_close_asof(cluster, side, asof_ts, ts, high, low, close, records) -> None:
    """Resolves sweep state for `cluster` using only candles up to (but
    not including) `asof_ts` -- called before deciding whether a new
    swing confirmed AT `asof_ts` may merge into this cluster, so the
    merge decision itself never uses information from `asof_ts` onward."""
    rec = records[cluster["record_idx"]]
    if rec["state"] != "ACTIVE":
        return
    walk_start = cluster["creation_candle_index"] + 1
    # last visible index strictly before asof_ts
    visible = ts[ts < asof_ts]
    if visible.empty:
        return
    last_visible_index = visible.index[-1]
    if walk_start > last_visible_index:
        return
    price = rec["price"]
    w_high = high[walk_start:last_visible_index + 1]
    w_low = low[walk_start:last_visible_index + 1]
    w_close = close[walk_start:last_visible_index + 1]

    if side == "buy_side":
        touch_mask = w_high >= price
        sweep_mask = (w_high > price) & (w_close < price)
    else:
        touch_mask = w_low <= price
        sweep_mask = (w_low < price) & (w_close > price)

    if touch_mask.any() and rec["first_touch_timestamp"] is None:
        first_touch_idx = int(np.argmax(touch_mask))
        rec["first_touch_timestamp"] = ts.iloc[walk_start + first_touch_idx]

    if sweep_mask.any():
        sweep_idx = int(np.argmax(sweep_mask))
        rec["swept_timestamp"] = ts.iloc[walk_start + sweep_idx]
        rec["state"] = "SWEPT"
