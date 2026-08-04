"""
Reference Level Engine.

Maintains "known-in-advance" reference levels every strategy can query
without recomputing them: Previous Day High/Low, Previous Week High/Low,
and the weekend gap (Friday close -> Sunday/Monday reopen).

Anti-look-ahead rule: a day's high/low is not final until that day has
finished. Therefore PDH/PDL for calendar day D is only marked usable
(`available_from`) starting at the OPEN of day D+1 -- never during day D
itself. The same logic applies to PWH/PWL at the week boundary.

Daily/weekly bars are built with the existing resampler
(src.data.resample.resample_ohlc) so there is no duplicated OHLC
aggregation logic.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.data.resample import resample_ohlc


def _daily_weekly_bars(df: pd.DataFrame, timestamp_col: str = "timestamp"):
    daily = resample_ohlc(df, "1D", timestamp_col=timestamp_col)
    weekly = resample_ohlc(df, "W-SUN", timestamp_col=timestamp_col)
    return daily, weekly


def compute_reference_levels(df: pd.DataFrame, timestamp_col: str = "timestamp") -> pd.DataFrame:
    """Returns a long-format DataFrame:
        level_type, value, reference_period_start, available_from, metadata
    covering PDH, PDL, PWH, PWL. Weekend gaps are returned separately by
    `compute_weekend_gaps` (they carry richer, gap-specific fields).
    """
    daily, weekly = _daily_weekly_bars(df, timestamp_col)
    records = []

    for i in range(1, len(daily)):
        prev = daily.iloc[i - 1]
        cur = daily.iloc[i]
        records.append({
            "level_type": "PDH", "value": float(prev["high"]),
            "reference_period_start": prev[timestamp_col],
            "available_from": cur[timestamp_col],
            "metadata": {"reference_day": str(prev[timestamp_col].date())},
        })
        records.append({
            "level_type": "PDL", "value": float(prev["low"]),
            "reference_period_start": prev[timestamp_col],
            "available_from": cur[timestamp_col],
            "metadata": {"reference_day": str(prev[timestamp_col].date())},
        })

    for i in range(1, len(weekly)):
        prev = weekly.iloc[i - 1]
        cur = weekly.iloc[i]
        records.append({
            "level_type": "PWH", "value": float(prev["high"]),
            "reference_period_start": prev[timestamp_col],
            "available_from": cur[timestamp_col],
            "metadata": {"reference_week_start": str(prev[timestamp_col].date())},
        })
        records.append({
            "level_type": "PWL", "value": float(prev["low"]),
            "reference_period_start": prev[timestamp_col],
            "available_from": cur[timestamp_col],
            "metadata": {"reference_week_start": str(prev[timestamp_col].date())},
        })

    columns = ["level_type", "value", "reference_period_start", "available_from", "metadata"]
    out = pd.DataFrame.from_records(records, columns=columns)
    if not out.empty:
        out = out.sort_values("available_from").reset_index(drop=True)
    return out


def compute_weekend_gaps(
    df: pd.DataFrame,
    timestamp_col: str = "timestamp",
    as_of_index: int | None = None,
    min_gap_hours: float = 20.0,
) -> pd.DataFrame:
    """Detect weekend gaps: a time jump of at least `min_gap_hours` between
    consecutive candles where the earlier candle falls on a Friday (weekday
    4) and the later candle reopens on Saturday/Sunday/Monday.

    For each gap, records Friday close, Sunday/Monday reopen (open),
    gap size/%/direction, and tracks how much of the gap has since been
    "filled" (price trading back to the Friday close level) plus its age
    in candles.
    """
    n = len(df)
    last_index = (n - 1) if as_of_index is None else min(as_of_index, n - 1)
    ts = df[timestamp_col].reset_index(drop=True)
    ts_list = ts.tolist()
    open_ = df["open"].to_numpy()
    close = df["close"].to_numpy()
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()

    records = []
    gap_seq = 0

    # Task 7.4 PERFORMANCE NOTE: this originally scanned every candle
    # (range(1, last_index+1)) with two pandas .iloc[] lookups per
    # iteration, even though weekend gaps occur roughly once a week --
    # profiling showed this among the largest cumulative-time contributors
    # for zero return on ~99.98% of candles. Candidate gap boundaries are
    # now found with a single vectorized diff()/weekday() pass, and the
    # loop below only visits those candidates (identical results, since
    # the candidate condition is unchanged: delta >= min_gap_hours AND the
    # earlier candle is a Friday).
    delta_all = ts.diff()
    min_delta = pd.Timedelta(hours=min_gap_hours)
    is_friday_prev = ts.shift(1).dt.weekday == 4
    candidate_mask = (delta_all >= min_delta) & is_friday_prev
    candidate_positions = np.flatnonzero(candidate_mask.to_numpy())
    candidate_positions = candidate_positions[
        (candidate_positions >= 1) & (candidate_positions <= last_index)
    ]

    for i in candidate_positions:
        i = int(i)
        gap_seq += 1
        friday_close = float(close[i - 1])
        reopen_open = float(open_[i])
        gap_size = reopen_open - friday_close
        gap_pct = (gap_size / friday_close * 100.0) if friday_close != 0 else 0.0
        gap_direction = "up" if gap_size > 0 else ("down" if gap_size < 0 else "flat")

        filled_pct = 0.0
        if i <= last_index:
            w_high = high[i:last_index + 1]
            w_low = low[i:last_index + 1]
            if gap_direction == "up":
                # filled when price trades back down to the friday close
                deepest = reopen_open - w_low.min()
                filled_pct = round(100.0 * max(0.0, min(deepest, abs(gap_size))) / abs(gap_size), 2) if gap_size != 0 else 100.0
            elif gap_direction == "down":
                deepest = w_high.max() - reopen_open
                filled_pct = round(100.0 * max(0.0, min(deepest, abs(gap_size))) / abs(gap_size), 2) if gap_size != 0 else 100.0
            else:
                filled_pct = 100.0

        records.append({
            "gap_id": f"WEEKEND_GAP_{gap_seq}",
            "friday_close_timestamp": ts_list[i - 1],
            "friday_close": friday_close,
            "reopen_timestamp": ts_list[i],
            "reopen_open": reopen_open,
            "gap_size": gap_size,
            "gap_pct": gap_pct,
            "gap_direction": gap_direction,
            "gap_filled_pct": filled_pct,
            "gap_age_candles": last_index - i,
            "state": "FILLED" if filled_pct >= 100.0 else ("PARTIALLY_FILLED" if filled_pct > 0 else "OPEN"),
        })

    columns = [
        "gap_id", "friday_close_timestamp", "friday_close", "reopen_timestamp",
        "reopen_open", "gap_size", "gap_pct", "gap_direction", "gap_filled_pct",
        "gap_age_candles", "state",
    ]
    return pd.DataFrame.from_records(records, columns=columns)
