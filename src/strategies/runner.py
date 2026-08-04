"""
Strategy Engine runner: builds one shared MarketContext, runs every
enabled strategy over it, deduplicates, and exports the combined
signals.parquet dataset. This module makes NO trading decisions of its
own -- it only orchestrates the five independent strategy modules.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from config.settings import (
    DEFAULT_S1_CONFIG, DEFAULT_S2_CONFIG, DEFAULT_S3_CONFIG, DEFAULT_S4_CONFIG, DEFAULT_S5_CONFIG,
)
from src.strategies.context import MarketContext
from src.strategies.common import dedupe_signals
from src.strategies import s1_monday_gap, s2_third_bos, s3_liquidity_sweep, s4_pdh_pdl_sweep, s5_asian_range_sweep

STRATEGY_MODULES = {
    "S1": (s1_monday_gap, DEFAULT_S1_CONFIG),
    "S2": (s2_third_bos, DEFAULT_S2_CONFIG),
    "S3": (s3_liquidity_sweep, DEFAULT_S3_CONFIG),
    "S4": (s4_pdh_pdl_sweep, DEFAULT_S4_CONFIG),
    "S5": (s5_asian_range_sweep, DEFAULT_S5_CONFIG),
}


def run_strategies(context: MarketContext, configs: dict | None = None, progress_cb=None) -> list:
    """configs: optional {"S1": S1Config(...), ...} overrides. Any
    strategy config with `enabled=False` is skipped entirely -- and every
    strategy is independent, so disabling one never affects another's
    output.

    progress_cb: optional Task 7.4 Objective 5 hook, called as
    `progress_cb(strategy_id)` after each strategy module finishes. Purely
    an observability side-channel for progress instrumentation -- it does
    not receive or influence any signal data, so it cannot affect trading
    logic. Defaults to None (no-op), so every existing caller is
    unaffected."""
    configs = configs or {}
    all_signals = []
    for strategy_id, (module, default_config) in STRATEGY_MODULES.items():
        config = configs.get(strategy_id, default_config)
        if not getattr(config, "enabled", True):
            if progress_cb is not None:
                progress_cb(strategy_id)
            continue
        all_signals.extend(module.generate_signals(context, config))
        if progress_cb is not None:
            progress_cb(strategy_id)
    return dedupe_signals(all_signals)


def signals_to_dataframe(signals: list) -> pd.DataFrame:
    rows = [s.to_dict() for s in signals]
    return pd.DataFrame(rows)


def save_signals(signals: list, path: str | Path) -> None:
    df = signals_to_dataframe(signals)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    out = df.copy()
    for col in ("entry_zone", "reason_codes", "confluence_snapshot", "risk_reference", "metadata"):
        if col in out.columns:
            out[col] = out[col].apply(lambda v: json.dumps(v, default=str))
    out.to_parquet(p, index=False)
