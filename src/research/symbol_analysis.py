"""
Symbol Analysis: compares performance across whichever symbols the
caller has run experiments for (the task brief names EURUSD, GBPUSD,
USDJPY, XAUUSD, NAS100, US30, BTCUSD as examples -- "use available
datasets", so this module works on however many symbols were actually
supplied, not a hardcoded list).
"""

from __future__ import annotations

import pandas as pd

from src.research.analysis_utils import group_metrics


def analyze_symbols(trades: list, starting_balance: float = 10_000.0) -> pd.DataFrame:
    df = group_metrics(trades, lambda t: t.symbol, starting_balance)
    return df.rename(columns={"group": "symbol"})


def rank_symbols(trades: list, starting_balance: float = 10_000.0, metric: str = "expectancy") -> pd.DataFrame:
    df = analyze_symbols(trades, starting_balance)
    if df.empty:
        return df
    return df.sort_values(metric, ascending=False).reset_index(drop=True)
