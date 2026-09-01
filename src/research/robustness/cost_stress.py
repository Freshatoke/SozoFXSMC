"""
Task 12 Phase 9 -- Cost and execution stress testing.

Re-runs a promising configuration under increasingly hostile
`ExecutionConfig`/`EntryConfig` assumptions -- reusing the EXISTING
`ExecutionConfig` dataclass and `run_backtest`'s existing cost-application
logic (`src.backtest.execution`) unchanged. This module only constructs
progressively worse config VALUES; it introduces no new cost-modeling
code, per this task's "do not duplicate the backtest engine" instruction.
"""

from __future__ import annotations

from dataclasses import replace

import pandas as pd

from config.settings import DEFAULT_EXECUTION_CONFIG, DEFAULT_ENTRY_CONFIG
from src.research.robustness.search_engine import run_one_configuration

# Named, ordered scenarios -- baseline first, then progressively worse.
# Multipliers are applied to the DEFAULT_EXECUTION_CONFIG values, not
# arbitrary absolute numbers, so this scales sensibly for any symbol's
# pip size/typical spread.
STRESS_SCENARIOS = {
    "baseline": {"spread_mult": 1.0, "slippage_mult": 1.0, "commission_mult": 1.0, "latency_candles": 0},
    "elevated_spread": {"spread_mult": 2.0, "slippage_mult": 1.0, "commission_mult": 1.0, "latency_candles": 0},
    "elevated_slippage": {"spread_mult": 1.0, "slippage_mult": 3.0, "commission_mult": 1.0, "latency_candles": 0},
    "delayed_entry": {"spread_mult": 1.0, "slippage_mult": 1.0, "commission_mult": 1.0, "latency_candles": 3},
    "higher_commission": {"spread_mult": 1.0, "slippage_mult": 1.0, "commission_mult": 2.0, "latency_candles": 0},
    "adverse_execution": {"spread_mult": 2.0, "slippage_mult": 3.0, "commission_mult": 2.0, "latency_candles": 3},
}


def run_cost_stress_test(context, m1_slice: pd.DataFrame, params: dict, symbol: str, **run_kwargs) -> dict:
    """Returns {scenario_name: ConfigResult}. `adverse_execution` combines
    every hostile assumption at once -- a configuration whose expectancy
    survives THAT scenario is the one worth taking seriously; the
    intermediate scenarios exist to show WHICH friction source matters
    most, per this task's explicit reporting requirement."""
    results = {}
    for name, mult in STRESS_SCENARIOS.items():
        exec_cfg = replace(
            DEFAULT_EXECUTION_CONFIG,
            spread_pips=DEFAULT_EXECUTION_CONFIG.spread_pips * mult["spread_mult"],
            slippage_pips=DEFAULT_EXECUTION_CONFIG.slippage_pips * mult["slippage_mult"],
            commission_per_lot=DEFAULT_EXECUTION_CONFIG.commission_per_lot * mult["commission_mult"],
            latency_candles=mult["latency_candles"],
        )
        results[name] = run_one_configuration(
            context, m1_slice, params, symbol, execution_config=exec_cfg, period=f"cost_stress_{name}", **run_kwargs,
        )
    return results


def summarize_cost_stress(results: dict) -> dict:
    baseline = results.get("baseline")
    if baseline is None or baseline.num_trades == 0:
        return {"survives_adverse_execution": False, "reason": "no baseline trades to compare against"}
    adverse = results.get("adverse_execution")
    survives = adverse is not None and adverse.expectancy > 0 and adverse.num_trades > 0
    degradation_pct = (
        round(100.0 * (1 - adverse.expectancy / baseline.expectancy), 2)
        if adverse is not None and baseline.expectancy > 0 else None
    )
    return {
        "survives_adverse_execution": survives, "baseline_expectancy": baseline.expectancy,
        "adverse_expectancy": adverse.expectancy if adverse else None, "degradation_pct": degradation_pct,
        "per_scenario": {name: {"expectancy": r.expectancy, "num_trades": r.num_trades, "profit_factor": r.profit_factor} for name, r in results.items()},
    }
