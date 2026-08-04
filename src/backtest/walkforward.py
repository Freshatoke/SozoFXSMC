"""
Walk-Forward dataset splitting.

Parameter optimisation itself is explicitly out of scope for Task 4 (see
the task brief) -- this module only prepares the architecture for it by
providing a deterministic, chronological split of a historical dataset
into Training / Validation / Out-of-Sample periods, and a helper to tag
which period any given timestamp (signal or trade) falls into. A future
optimisation task can fit parameters on `train`, tune on `validation`,
and report final performance only on `out_of_sample`.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class WalkForwardSplit:
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    validation_start: pd.Timestamp
    validation_end: pd.Timestamp
    out_of_sample_start: pd.Timestamp
    out_of_sample_end: pd.Timestamp

    def period_of(self, timestamp: pd.Timestamp) -> str:
        if self.train_start <= timestamp < self.train_end:
            return "train"
        if self.validation_start <= timestamp < self.validation_end:
            return "validation"
        if self.out_of_sample_start <= timestamp <= self.out_of_sample_end:
            return "out_of_sample"
        return "outside_range"


def split_dataset(m1: pd.DataFrame, train_pct: float = 0.6, validation_pct: float = 0.2, timestamp_col: str = "timestamp") -> WalkForwardSplit:
    """Chronological split by ROW COUNT (not calendar time), so each
    period gets a proportional share of actual trading activity rather
    than being skewed by weekends/holidays with no data."""
    if not (0 < train_pct < 1) or not (0 < validation_pct < 1) or train_pct + validation_pct >= 1:
        raise ValueError("train_pct + validation_pct must be < 1, and both must be in (0, 1)")

    n = len(m1)
    train_end_idx = int(n * train_pct)
    validation_end_idx = int(n * (train_pct + validation_pct))

    ts = m1[timestamp_col]
    return WalkForwardSplit(
        train_start=ts.iloc[0], train_end=ts.iloc[train_end_idx],
        validation_start=ts.iloc[train_end_idx], validation_end=ts.iloc[validation_end_idx],
        out_of_sample_start=ts.iloc[validation_end_idx], out_of_sample_end=ts.iloc[-1],
    )


def split_dataframes(m1: pd.DataFrame, split: WalkForwardSplit, timestamp_col: str = "timestamp") -> dict:
    """Returns {"train": df, "validation": df, "out_of_sample": df}."""
    ts = m1[timestamp_col]
    return {
        "train": m1[(ts >= split.train_start) & (ts < split.train_end)].reset_index(drop=True),
        "validation": m1[(ts >= split.validation_start) & (ts < split.validation_end)].reset_index(drop=True),
        "out_of_sample": m1[(ts >= split.out_of_sample_start) & (ts <= split.out_of_sample_end)].reset_index(drop=True),
    }


def tag_trades_by_period(trades: list, split: WalkForwardSplit) -> None:
    """Mutates each trade's `metadata["walk_forward_period"]` in place."""
    for t in trades:
        reference_ts = t.entry_timestamp or t.signal_timestamp
        t.metadata["walk_forward_period"] = split.period_of(reference_ts)
