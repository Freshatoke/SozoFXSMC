"""
Task 12 Phase 1 -- Bounded, seeded parameter search engine.

Deliberately NOT a duplicate of `src.backtest.engine` or
`src.research.parameter_sweep`: this module only (a) samples a bounded
number of configurations from a parameter space, (b) gives each one a
deterministic ID, and (c) runs each through the EXISTING, unmodified
`generate_gap_reversion_signals` -> `run_backtest` ->
`compute_performance_metrics` pipeline, once per configuration. All the
actual signal generation and trade simulation is reused code.

Bounded by design: `run_search` refuses to run more than
`MAX_CONFIGURATIONS` (5,000, the top of this task's own instructed range)
without an explicit `allow_large_search=True` override -- the video's
own cautionary finding (25,000 configurations, ~5% expected false
positives at an uncorrected p<0.05 bar) is exactly why this task's brief
caps the FIRST experiment at 1,000-5,000, and this cap is enforced in
code, not just in a docstring.
"""

from __future__ import annotations

import hashlib
import json
import random as _random
from dataclasses import dataclass, field, replace, fields
from typing import Optional

import pandas as pd

from src.strategies.context import MarketContext
from src.backtest.engine import run_backtest
from src.backtest.metrics import compute_performance_metrics
from src.research.robustness.gap_signals import GapResearchConfig, generate_gap_reversion_signals

MAX_CONFIGURATIONS = 5_000


def config_id(params: dict) -> str:
    """Deterministic configuration ID: same parameters ALWAYS produce the
    same ID, on any machine, any run -- this is what makes an experiment
    reproducible from the registry (Phase 10) rather than merely
    re-runnable-and-hopefully-the-same."""
    blob = json.dumps(params, sort_keys=True, default=str)
    return "CFG_" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def sample_configurations(param_space: dict, n: int, seed: int) -> list:
    """param_space: {field_name: [candidate values]}. Returns up to `n`
    DISTINCT parameter dicts (deduplicated by config_id), sampled via a
    seeded `random.Random` -- same seed always produces the same set,
    on any machine (Python's `random.Random` sampling algorithm is part
    of the language spec, not platform-dependent, unlike e.g. hash-seed
    randomization of plain `hash()`)."""
    rng = _random.Random(seed)
    field_names = list(param_space.keys())
    seen_ids = set()
    configs = []
    # Bounded retry: the space may be smaller than n, or heavy duplication
    # may occur near the space's actual size -- stop once retries are
    # clearly not finding anything new, rather than looping forever.
    max_attempts = n * 20
    attempts = 0
    while len(configs) < n and attempts < max_attempts:
        attempts += 1
        candidate = {f: rng.choice(param_space[f]) for f in field_names}
        cid = config_id(candidate)
        if cid in seen_ids:
            continue
        seen_ids.add(cid)
        configs.append(candidate)
    return configs


@dataclass
class ConfigResult:
    config_id: str
    parameters: dict
    symbol: str
    timeframe: str
    date_range: tuple
    num_trades: int
    win_rate: float
    expectancy: float
    profit_factor: float
    total_r: float
    max_drawdown_pct: float
    sharpe_ratio: float
    sortino_ratio: float
    average_trade: float
    execution_config: dict
    num_observations: int
    period: str            # "train_validation" or "out_of_sample"
    raw_metrics: dict = field(default_factory=dict)
    error: Optional[str] = None   # populated, never silently dropped, if this config failed to run

    def to_row(self) -> dict:
        return {
            "config_id": self.config_id, "symbol": self.symbol, "timeframe": self.timeframe,
            "date_start": str(self.date_range[0]), "date_end": str(self.date_range[1]),
            "num_trades": self.num_trades, "win_rate": self.win_rate, "expectancy": self.expectancy,
            "profit_factor": self.profit_factor, "total_r": self.total_r,
            "max_drawdown_pct": self.max_drawdown_pct, "sharpe_ratio": self.sharpe_ratio,
            "sortino_ratio": self.sortino_ratio, "average_trade": self.average_trade,
            "num_observations": self.num_observations, "period": self.period, "error": self.error,
            **{f"param.{k}": v for k, v in self.parameters.items()},
        }


def run_one_configuration(context: MarketContext, m1_slice: pd.DataFrame, params: dict, symbol: str,
                           execution_config=None, stop_config=None, tp_config=None, risk_config=None,
                           management_config=None, period: str = "train_validation",
                           starting_balance: float = 10_000.0) -> ConfigResult:
    """Runs ONE configuration against ONE data slice (train+validation, OR
    out-of-sample -- caller decides which, this function has no opinion
    and no access control of its own; `ResearchDataset` is what enforces
    the OOS lock at the call-site level)."""
    from config.settings import DEFAULT_EXECUTION_CONFIG, DEFAULT_STOP_LOSS_CONFIG, DEFAULT_TAKE_PROFIT_CONFIG, DEFAULT_RISK_CONFIG, DEFAULT_MANAGEMENT_CONFIG
    execution_config = execution_config or DEFAULT_EXECUTION_CONFIG
    stop_config = stop_config or replace(DEFAULT_STOP_LOSS_CONFIG, method=params.get("stop_reference", DEFAULT_STOP_LOSS_CONFIG.method))
    tp_config = tp_config or replace(DEFAULT_TAKE_PROFIT_CONFIG, method=params.get("target_style", DEFAULT_TAKE_PROFIT_CONFIG.method),
                                      risk_reward=params.get("risk_reward", DEFAULT_TAKE_PROFIT_CONFIG.risk_reward))
    risk_config = risk_config or DEFAULT_RISK_CONFIG
    management_config = management_config or DEFAULT_MANAGEMENT_CONFIG

    cid = config_id(params)
    date_range = (m1_slice["timestamp"].iloc[0], m1_slice["timestamp"].iloc[-1]) if not m1_slice.empty else (None, None)

    try:
        cfg = GapResearchConfig(**{k: v for k, v in params.items() if k in {f.name for f in fields(GapResearchConfig)}})
        signals = generate_gap_reversion_signals(context, cfg)
        trades = run_backtest(
            signals, m1_slice, context=context, stop_config=stop_config, tp_config=tp_config,
            execution_config=execution_config, risk_config=risk_config, management_config=management_config,
        )
        metrics = compute_performance_metrics(trades, starting_balance)
        r_values = metrics["r_multiple_distribution"]["values"]
        return ConfigResult(
            config_id=cid, parameters=params, symbol=symbol, timeframe="M1", date_range=date_range,
            num_trades=metrics["signal_utilization"]["closed_trades"], win_rate=metrics["win_rate"],
            expectancy=metrics["expectancy"], profit_factor=metrics["profit_factor"],
            total_r=round(sum(r_values), 4) if r_values else 0.0, max_drawdown_pct=metrics["max_drawdown_pct"],
            sharpe_ratio=metrics["sharpe_ratio"], sortino_ratio=metrics["sortino_ratio"],
            average_trade=metrics["expectancy"], execution_config=asdict_safe(execution_config),
            num_observations=len(m1_slice), period=period, raw_metrics=metrics,
        )
    except Exception as exc:
        # Task 12 Phase 12: "Never silently drop failed configurations."
        return ConfigResult(
            config_id=cid, parameters=params, symbol=symbol, timeframe="M1", date_range=date_range,
            num_trades=0, win_rate=0.0, expectancy=0.0, profit_factor=0.0, total_r=0.0,
            max_drawdown_pct=0.0, sharpe_ratio=0.0, sortino_ratio=0.0, average_trade=0.0,
            execution_config={}, num_observations=len(m1_slice), period=period, error=str(exc),
        )


def asdict_safe(obj) -> dict:
    from dataclasses import asdict, is_dataclass
    return asdict(obj) if is_dataclass(obj) else dict(obj) if isinstance(obj, dict) else {}


def run_search(param_space: dict, n_configs: int, seed: int, context: MarketContext, m1_slice: pd.DataFrame,
               symbol: str, allow_large_search: bool = False, **kwargs) -> list:
    """The bounded search entry point. Raises if `n_configs` exceeds
    `MAX_CONFIGURATIONS` unless explicitly overridden -- see module
    docstring."""
    if n_configs > MAX_CONFIGURATIONS and not allow_large_search:
        raise ValueError(
            f"run_search: n_configs={n_configs} exceeds MAX_CONFIGURATIONS={MAX_CONFIGURATIONS}. "
            f"Task 12's brief explicitly caps the first experiment at 1,000-5,000 configurations "
            f"(NOT the video's 25,000) -- pass allow_large_search=True to override deliberately."
        )
    configs = sample_configurations(param_space, n_configs, seed)
    results = []
    for params in configs:
        results.append(run_one_configuration(context, m1_slice, params, symbol, period="train_validation", **kwargs))
    return results
