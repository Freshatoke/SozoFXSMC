"""
Session Analysis: performance by Asian/London/New York session, plus
session-overlap trades (entries falling inside more than one configured
session window at once, e.g. the London/New York overlap).
"""

from __future__ import annotations

import pandas as pd

from src.research.analysis_utils import group_metrics


def _sessions_active_at(context, timestamp) -> list:
    return [name for name in context.session_config.windows if context.session_active_asof(name, timestamp)]


def label_trade_sessions(trades: list, context) -> None:
    """Mutates each trade's `metadata["active_sessions"]` (list of every
    session active at entry) and `metadata["session_overlap"]` (bool)."""
    for t in trades:
        if t.entry_timestamp is None:
            continue
        active = _sessions_active_at(context, t.entry_timestamp)
        t.metadata["active_sessions"] = active
        t.metadata["session_overlap"] = len(active) > 1


def analyze_sessions(trades: list, starting_balance: float = 10_000.0) -> pd.DataFrame:
    df = group_metrics(trades, lambda t: t.session or "unknown", starting_balance)
    return df.rename(columns={"group": "session"})


def analyze_session_overlaps(trades: list, context, starting_balance: float = 10_000.0) -> pd.DataFrame:
    label_trade_sessions(trades, context)

    def key(t):
        sessions = t.metadata.get("active_sessions", [])
        if not sessions:
            return "none"
        return " + ".join(sorted(sessions)) if len(sessions) > 1 else sessions[0]

    df = group_metrics(trades, key, starting_balance)
    return df.rename(columns={"group": "session_combination"})
