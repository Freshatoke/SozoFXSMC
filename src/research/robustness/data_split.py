"""
Task 12 Phase 2 -- OOS-lock enforcement.

Reuses `src.backtest.walkforward` (`split_dataset`, `split_dataframes`,
`tag_trades_by_period`) UNCHANGED for the actual chronological split --
that module already implements exactly what this phase asks for (strict
temporal TRAIN/VALIDATION/OUT_OF_SAMPLE split, no shuffling). What this
module adds is a runtime GUARD: a `ResearchDataset` object that makes it
structurally impossible for the search engine to accidentally read the
out-of-sample DataFrame before `unlock_out_of_sample()` is explicitly
called -- accessing `.out_of_sample` before that raises, rather than
silently returning data a careless caller could leak into configuration
selection.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from src.backtest.walkforward import WalkForwardSplit, split_dataset, split_dataframes


class OutOfSampleLockedError(RuntimeError):
    """Raised when code tries to read the OOS DataFrame before the
    configuration-selection process has explicitly declared itself done."""


@dataclass
class ResearchDataset:
    symbol: str
    split: WalkForwardSplit
    _frames: dict
    _oos_unlocked: bool = False

    @property
    def train(self) -> pd.DataFrame:
        return self._frames["train"]

    @property
    def validation(self) -> pd.DataFrame:
        return self._frames["validation"]

    @property
    def out_of_sample(self) -> pd.DataFrame:
        if not self._oos_unlocked:
            raise OutOfSampleLockedError(
                "out_of_sample is locked -- configuration selection must be complete before calling "
                "unlock_out_of_sample(). Reading OOS data during selection is exactly the data-snooping "
                "failure mode this framework exists to prevent (see docs/RESEARCH_ROBUSTNESS_FRAMEWORK.md)."
            )
        return self._frames["out_of_sample"]

    def unlock_out_of_sample(self, reason: str) -> None:
        """`reason` is required and logged -- there is no anonymous unlock,
        so a report can always state WHY/WHEN the OOS period was opened."""
        if not reason:
            raise ValueError("unlock_out_of_sample requires a non-empty reason (e.g. 'configuration selection frozen after N=1000 configs')")
        self._oos_unlocked = True
        self._unlock_reason = reason

    def date_ranges(self) -> dict:
        s = self.split
        return {
            "train": (str(s.train_start), str(s.train_end)),
            "validation": (str(s.validation_start), str(s.validation_end)),
            "out_of_sample": (str(s.out_of_sample_start), str(s.out_of_sample_end)),
        }


def build_research_dataset(symbol: str, m1: pd.DataFrame, train_pct: float = 0.6, validation_pct: float = 0.2) -> ResearchDataset:
    split = split_dataset(m1, train_pct=train_pct, validation_pct=validation_pct)
    frames = split_dataframes(m1, split)
    return ResearchDataset(symbol=symbol, split=split, _frames=frames)
