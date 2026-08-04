"""
Research Engine: the reproducible unit of work every other module in
`src/research/` builds on. An `Experiment` records everything needed to
reproduce a result byte-for-byte: which strategy(ies), which
configuration, which dataset, which parameter values, plus the resulting
metrics and any free-text notes.

Reproducibility is structural, not incidental: `run_experiment` never
reads wall-clock time, randomness, or any global mutable state -- given
the same symbol/m1/strategy/config/parameter_set inputs, it always
produces the same `results` (verified in
`tests/test_research.py::test_experiment_reproducibility`).
`research_id` is a deterministic hash of exactly those inputs, so two
experiments with identical inputs always get the same id, and any
difference in id reveals *some* input differed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

import pandas as pd

from config.settings import DEFAULT_RISK_CONFIG
from src.strategies.context import MarketContext
from src.strategies.runner import run_strategies
from src.backtest.engine import run_backtest
from src.backtest.metrics import compute_performance_metrics


def _json_default(obj):
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    return str(obj)


def _stable_hash(payload: dict) -> str:
    blob = json.dumps(payload, sort_keys=True, default=_json_default)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


@dataclass
class Experiment:
    research_id: str
    experiment_name: str
    timestamp: pd.Timestamp
    strategy: Any                  # strategy_id, list of strategy_ids, or "ALL"
    configuration: dict             # serializable view of every config used
    dataset: str                    # e.g. "EURUSD:2024-01-01/2024-01-10"
    parameter_set: dict             # the specific parameter values under test (may be empty)
    results: dict = field(default_factory=dict)
    notes: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


def _dataset_id(symbol: str, m1: pd.DataFrame) -> str:
    if m1.empty:
        return f"{symbol}:empty"
    return f"{symbol}:{m1['timestamp'].iloc[0].isoformat()}/{m1['timestamp'].iloc[-1].isoformat()}"


def run_experiment(
    experiment_name: str,
    symbol: str,
    m1: pd.DataFrame,
    strategy_configs: Optional[dict] = None,
    strategy_filter: Optional[list] = None,
    backtest_kwargs: Optional[dict] = None,
    starting_balance: float = DEFAULT_RISK_CONFIG.starting_balance,
    parameter_set: Optional[dict] = None,
    notes: str = "",
    context: Optional[MarketContext] = None,
) -> Experiment:
    """Runs Task 3 signal generation + Task 4 backtest simulation for one
    configuration and packages the result as a reproducible `Experiment`.

    `strategy_configs`: {"S1": S1Config(...), ...} overrides passed straight
    to `run_strategies` -- this is how parameter sweeps vary a strategy's
    thresholds without touching any Task 3 code.
    `strategy_filter`: optional list of strategy_ids to keep after signal
    generation (e.g. run just S3 in isolation).
    `backtest_kwargs`: forwarded to `run_backtest` (entry_config,
    stop_config, tp_config, execution_config, risk_config, management_config).
    `context`: pass an already-built `MarketContext` for the same
    (symbol, m1) to reuse its cached swings/structure/OB/FVG/liquidity
    computations across many experiments -- essential for parameter
    sweeps, which otherwise re-derive the entire market structure from
    scratch for every parameter value tested. `src.research.parameter_sweep`
    always does this; build your own context and pass it here if calling
    `run_experiment` directly in a loop.
    """
    backtest_kwargs = backtest_kwargs or {}
    strategy_configs = strategy_configs or {}
    parameter_set = parameter_set or {}

    context = context or MarketContext(symbol=symbol, m1=m1)
    signals = run_strategies(context, configs=strategy_configs)
    if strategy_filter is not None:
        signals = [s for s in signals if s.strategy_id in strategy_filter]

    trades = run_backtest(signals, m1, context=context, **backtest_kwargs)
    metrics = compute_performance_metrics(trades, starting_balance)

    configuration = {
        "strategy_configs": {k: asdict(v) if hasattr(v, "__dataclass_fields__") else v for k, v in strategy_configs.items()},
        "backtest_kwargs": {k: (asdict(v) if hasattr(v, "__dataclass_fields__") else v) for k, v in backtest_kwargs.items()},
        "starting_balance": starting_balance,
        "strategy_filter": strategy_filter,
    }
    dataset = _dataset_id(symbol, m1)

    research_id = _stable_hash({
        "experiment_name": experiment_name, "dataset": dataset,
        "configuration": configuration, "parameter_set": parameter_set,
    })

    return Experiment(
        research_id=research_id, experiment_name=experiment_name, timestamp=pd.Timestamp.now("UTC"),
        strategy=strategy_filter or "ALL", configuration=configuration, dataset=dataset,
        parameter_set=parameter_set,
        results={"metrics": metrics, "num_signals": len(signals), "num_trades": len(trades), "trades": trades},
        notes=notes,
    )
