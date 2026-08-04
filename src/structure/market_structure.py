"""
Stateful BOS / CHoCH market-structure detector.

Definitions (see docs/MARKET_STRUCTURE_SPEC.md for the full spec):

- Structure state is one of UNKNOWN, BULLISH, BEARISH.
- We track exactly one "active" (unbroken) confirmed swing high and one
  active confirmed swing low at any time -- the most recent confirmed swing
  of each type that has not yet been broken.
- A BREAK happens when a candle's CLOSE (never a wick) moves beyond the
  active level.
    - Breaking the active swing HIGH with a close above it:
        * BOS (bullish) if state is UNKNOWN or BULLISH (continuation)
        * CHoCH (bullish) if state is BEARISH (reversal)
      -> new state becomes BULLISH
    - Breaking the active swing LOW with a close below it:
        * BOS (bearish) if state is UNKNOWN or BEARISH (continuation)
        * CHoCH (bearish) if state is BULLISH (reversal)
      -> new state becomes BEARISH
- Once a level is broken it is retired immediately and cannot fire another
  event; the "active" level for that side stays empty until a new confirmed
  swing of that type appears in the swing stream.
- A swing can only become "active" once its confirmed_timestamp has been
  reached by the walk-forward candle-close clock -- this is what prevents
  look-ahead bias here.

This module deliberately uses a sequential state-machine walk over candles
(not row-wise DataFrame operations) because the logic is inherently
stateful; the walk itself is implemented over numpy arrays for performance.

PERFORMANCE NOTE (Task 7.4): the walk originally read `ts.iloc[i]` (pandas
Series positional access) and `swings_sorted.loc[swing_ptr]` /
`.loc[swing_ptr, col]` (pandas label-based row access) once per candle /
once per swing. Both go through substantial pandas indexing machinery per
call; profiling a real multi-month dataset showed this function among the
largest single cumulative-time contributors. Both are now plain Python
lists built ONCE before the loop (`ts.tolist()`, `swings_sorted.to_dict("records")`)
and indexed positionally (`ts_list[i]`, `swing_records[swing_ptr]`) --
identical values (a datetime64 Series' `.tolist()` yields the same
`pd.Timestamp` objects `.iloc[i]` would have, and `.to_dict("records")`
preserves every column), just without the pandas indexing overhead on
every one of the n/n_swings accesses.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from config.settings import StructureConfig, DEFAULT_STRUCTURE_CONFIG


@dataclass
class _ActiveLevel:
    price: float
    swing_timestamp: pd.Timestamp
    confirmed_timestamp: pd.Timestamp
    candle_index: int


def detect_structure_events(
    candles: pd.DataFrame,
    swings: pd.DataFrame,
    symbol: str,
    timeframe: str,
    config: StructureConfig = DEFAULT_STRUCTURE_CONFIG,
    timestamp_col: str = "timestamp",
) -> pd.DataFrame:
    """Walk candles in time order, feeding in swings as they become
    confirmed, and emit BOS/CHoCH events.

    candles: OHLC frame, sorted ascending, reset index, with `timestamp_col`
        as the candle OPEN time (bar close = next candle's open, but we only
        need the fact that by the time we process candle i, candle i has
        fully closed).
    swings: output of src.structure.swings.detect_swings for the SAME
        timeframe as `candles`.

    Returns a DataFrame of structure events, one row per BOS/CHoCH, ordered
    by break_candle_timestamp ascending.
    """
    require_close = config.require_close_beyond_level

    ts_list = candles[timestamp_col].reset_index(drop=True).tolist()
    close = candles["close"].to_numpy()
    high = candles["high"].to_numpy()
    low = candles["low"].to_numpy()
    n = len(candles)

    swings_sorted = swings.sort_values(["confirmed_timestamp", "candle_index"]).reset_index(drop=True)
    swing_records = swings_sorted.to_dict("records")
    swing_ptr = 0
    n_swings = len(swing_records)

    active_high: Optional[_ActiveLevel] = None
    active_low: Optional[_ActiveLevel] = None
    state = "UNKNOWN"

    events = []
    event_seq = 0
    # Tracks (price, swing_timestamp) pairs already broken so an identical
    # level can never re-fire even if it briefly became active again.
    broken_high_keys = set()
    broken_low_keys = set()

    for i in range(n):
        current_close_time = ts_list[i]  # candle i is being processed as "just closed"

        # Ingest all swings confirmed at or before this candle's close.
        while swing_ptr < n_swings and swing_records[swing_ptr]["confirmed_timestamp"] <= current_close_time:
            srow = swing_records[swing_ptr]
            key = (srow["swing_type"], round(float(srow["price"]), 10), srow["swing_timestamp"])
            if srow["swing_type"] == "high":
                if key not in broken_high_keys and (
                    active_high is None or srow["candle_index"] > active_high.candle_index
                ):
                    active_high = _ActiveLevel(
                        price=float(srow["price"]),
                        swing_timestamp=srow["swing_timestamp"],
                        confirmed_timestamp=srow["confirmed_timestamp"],
                        candle_index=int(srow["candle_index"]),
                    )
            else:
                if key not in broken_low_keys and (
                    active_low is None or srow["candle_index"] > active_low.candle_index
                ):
                    active_low = _ActiveLevel(
                        price=float(srow["price"]),
                        swing_timestamp=srow["swing_timestamp"],
                        confirmed_timestamp=srow["confirmed_timestamp"],
                        candle_index=int(srow["candle_index"]),
                    )
            swing_ptr += 1

        # --- bullish break (active high broken) ---
        if active_high is not None and (
            (require_close and close[i] > active_high.price)
            or (not require_close and high[i] > active_high.price)
        ):
            previous_state = state
            direction = "bullish"
            event_type = "CHoCH" if previous_state == "BEARISH" else "BOS"
            new_state = "BULLISH"

            event_seq += 1
            events.append({
                "event_id": f"{symbol}_{timeframe}_{event_seq}",
                "symbol": symbol,
                "timeframe": timeframe,
                "event_type": event_type,
                "direction": direction,
                "broken_level": active_high.price,
                "broken_swing_timestamp": active_high.swing_timestamp,
                "confirmation_timestamp": active_high.confirmed_timestamp,
                "break_candle_timestamp": current_close_time,
                "break_candle_close": float(close[i]),
                "previous_structure_state": previous_state,
                "new_structure_state": new_state,
                "price": float(close[i]),
                "swing_reference": active_high.swing_timestamp,
                "structure_before": previous_state,
                "structure_after": new_state,
                "metadata": {
                    "left": config.swing.left,
                    "right": config.swing.right,
                    "method": config.swing.method,
                },
            })

            broken_high_keys.add(("high", round(active_high.price, 10), active_high.swing_timestamp))
            active_high = None
            state = new_state

        # --- bearish break (active low broken) ---
        if active_low is not None and (
            (require_close and close[i] < active_low.price)
            or (not require_close and low[i] < active_low.price)
        ):
            previous_state = state
            direction = "bearish"
            event_type = "CHoCH" if previous_state == "BULLISH" else "BOS"
            new_state = "BEARISH"

            event_seq += 1
            events.append({
                "event_id": f"{symbol}_{timeframe}_{event_seq}",
                "symbol": symbol,
                "timeframe": timeframe,
                "event_type": event_type,
                "direction": direction,
                "broken_level": active_low.price,
                "broken_swing_timestamp": active_low.swing_timestamp,
                "confirmation_timestamp": active_low.confirmed_timestamp,
                "break_candle_timestamp": current_close_time,
                "break_candle_close": float(close[i]),
                "previous_structure_state": previous_state,
                "new_structure_state": new_state,
                "price": float(close[i]),
                "swing_reference": active_low.swing_timestamp,
                "structure_before": previous_state,
                "structure_after": new_state,
                "metadata": {
                    "left": config.swing.left,
                    "right": config.swing.right,
                    "method": config.swing.method,
                },
            })

            broken_low_keys.add(("low", round(active_low.price, 10), active_low.swing_timestamp))
            active_low = None
            state = new_state

    columns = [
        "event_id", "symbol", "timeframe", "event_type", "direction",
        "broken_level", "broken_swing_timestamp", "confirmation_timestamp",
        "break_candle_timestamp", "break_candle_close",
        "previous_structure_state", "new_structure_state",
        "price", "swing_reference", "structure_before", "structure_after", "metadata",
    ]
    return pd.DataFrame.from_records(events, columns=columns)


def save_events(events: pd.DataFrame, path: str) -> None:
    from pathlib import Path
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # metadata dict column -> parquet needs it stringified or use pyarrow struct;
    # simplest robust option: json-encode metadata for storage.
    import json
    out = events.copy()
    if "metadata" in out.columns:
        out["metadata"] = out["metadata"].apply(json.dumps)
    out.to_parquet(p, index=False)
