"""
Liquidity Engine.

Builds liquidity-level objects from confirmed swings (reusing
src.structure.swings -- no duplicated swing-detection logic) and tracks
their lifecycle: creation -> first touch -> sweep -> removal/archive.

Classification
--------------
- Every confirmed swing high is a **buy-side liquidity** pool (stops rest
  above it); every confirmed swing low is a **sell-side liquidity** pool.
- Swing highs (lows) whose prices cluster within
  `config.equal_level_tolerance` (relative) of each other are merged into a
  single **Equal Highs** (**Equal Lows**) level with `number_of_touches`
  equal to the cluster size -- these are the strongest liquidity pools.
- A level is **external liquidity** if its price equals the running
  extreme (highest confirmed swing-high price / lowest confirmed
  swing-low price) seen up to and including that swing -- i.e. it sits
  beyond the current internal dealing range. Otherwise it is **internal
  liquidity** (a minor swing inside the recently established range).

Liquidity Sweep definition
--------------------------
A sweep is a WICK-based event (unlike BOS/CHoCH, which are close-based):
a subsequent candle's high/low trades beyond the level, but that same
candle's CLOSE comes back on the origin side of the level. This matches
the classic "stop hunt" / liquidity grab pattern.

STATE MACHINE:
    ACTIVE -> SWEPT -> ARCHIVED
    ACTIVE  : level created, not yet swept.
    SWEPT   : a qualifying sweep candle occurred; level is considered used.
    ARCHIVED: `config.archive_after_candles` have elapsed since the sweep.
No level is ever deleted; only `state` changes.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from config.settings import LiquidityConfig, DEFAULT_LIQUIDITY_CONFIG, SwingConfig
from src.structure.swings import detect_swings


def _cluster_equal_levels(swings: pd.DataFrame, swing_type: str, tolerance: float) -> list[dict]:
    subset = swings[swings.swing_type == swing_type].sort_values("price").reset_index(drop=True)
    clusters = []
    current = []
    for _, row in subset.iterrows():
        if not current:
            current = [row]
            continue
        group_avg = np.mean([r["price"] for r in current])
        if abs(row["price"] - group_avg) <= tolerance * group_avg:
            current.append(row)
        else:
            clusters.append(current)
            current = [row]
    if current:
        clusters.append(current)
    return clusters


def detect_liquidity_levels(
    df: pd.DataFrame,
    symbol: str,
    timeframe: str,
    config: LiquidityConfig = DEFAULT_LIQUIDITY_CONFIG,
    timestamp_col: str = "timestamp",
    as_of_index: int | None = None,
) -> pd.DataFrame:
    n = len(df)
    last_index = (n - 1) if as_of_index is None else min(as_of_index, n - 1)
    ts = df[timestamp_col].reset_index(drop=True)
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    close = df["close"].to_numpy()

    swing_cfg = SwingConfig(left=config.swing_left, right=config.swing_right)
    swings = detect_swings(df, config=swing_cfg, timeframe_label=timeframe)
    swings = swings[swings["confirmed_timestamp"] <= ts.iloc[last_index]].reset_index(drop=True)

    records = []
    liq_seq = 0

    def running_extreme(swing_type: str, up_to_index: int) -> float:
        prior = swings[(swings.swing_type == swing_type) & (swings.candle_index <= up_to_index)]
        if prior.empty:
            return np.nan
        return prior["price"].max() if swing_type == "high" else prior["price"].min()

    for swing_type, side in (("high", "buy_side"), ("low", "sell_side")):
        clusters = _cluster_equal_levels(swings, swing_type, config.equal_level_tolerance)
        for cluster in clusters:
            liq_seq += 1
            is_equal = len(cluster) >= 2
            price = float(np.mean([r["price"] for r in cluster]))
            creation_timestamp = min(r["confirmed_timestamp"] for r in cluster)
            creation_candle_index = min(r["candle_index"] for r in cluster)
            last_swing_candle_index = max(r["candle_index"] for r in cluster)

            extreme = running_extreme(swing_type, last_swing_candle_index)
            is_external = bool(np.isclose(price, extreme, rtol=1e-9)) if pd.notna(extreme) else False

            level_type = ("equal_high" if is_equal else "swing_high") if swing_type == "high" else \
                         ("equal_low" if is_equal else "swing_low")

            # --- sweep detection: walk forward from creation ---
            walk_start = creation_candle_index + 1
            state = "ACTIVE"
            first_touch_timestamp = None
            swept_timestamp = None

            if walk_start <= last_index:
                w_high = high[walk_start:last_index + 1]
                w_low = low[walk_start:last_index + 1]
                w_close = close[walk_start:last_index + 1]

                if side == "buy_side":
                    touch_mask = w_high >= price
                    sweep_mask = (w_high > price) & (w_close < price)
                else:
                    touch_mask = w_low <= price
                    sweep_mask = (w_low < price) & (w_close > price)

                if touch_mask.any():
                    first_touch_idx = int(np.argmax(touch_mask))
                    first_touch_timestamp = ts.iloc[walk_start + first_touch_idx]

                if sweep_mask.any():
                    sweep_idx = int(np.argmax(sweep_mask))
                    swept_timestamp = ts.iloc[walk_start + sweep_idx]
                    state = "SWEPT"
                    if (last_index - (walk_start + sweep_idx)) >= config.archive_after_candles:
                        state = "ARCHIVED"

            records.append({
                "liquidity_id": f"{symbol}_{timeframe}_LIQ_{liq_seq}",
                "type": level_type,
                "side": side,
                "scope": "external" if is_external else "internal",
                "price": price,
                "number_of_touches": len(cluster),
                "strength": "strong" if len(cluster) >= config.min_touches_for_strength else "weak",
                "creation_timestamp": creation_timestamp,
                "creation_candle_index": creation_candle_index,
                "first_touch_timestamp": first_touch_timestamp,
                "swept_timestamp": swept_timestamp,
                "state": state,
            })

    columns = [
        "liquidity_id", "type", "side", "scope", "price", "number_of_touches",
        "strength", "creation_timestamp", "creation_candle_index",
        "first_touch_timestamp", "swept_timestamp", "state",
    ]
    out = pd.DataFrame.from_records(records, columns=columns)
    if not out.empty:
        out = out.sort_values("creation_timestamp").reset_index(drop=True)
    return out
