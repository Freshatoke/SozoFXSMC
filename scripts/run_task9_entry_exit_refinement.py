"""
Task 9 Phase 5/6 — Entry and Exit Refinement for S3/S4.

Uses the same EURUSD 3-month slice as Task 8 Tier 3 (identical scope
rationale: `run_experiment` regenerates all 5 strategies' signals per
sweep candidate regardless of `strategy_filter`, so a full 6.5-year
sweep would take hours -- see scripts/run_institutional_research.py's
Tier 3 docstring). Measures expectancy after each entry confirmation
refinement (Phase 5) and compares exit methods (Phase 6).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import (
    DEFAULT_S1_CONFIG, DEFAULT_S2_CONFIG, DEFAULT_S3_CONFIG, DEFAULT_S4_CONFIG, DEFAULT_S5_CONFIG,
    DEFAULT_TAKE_PROFIT_CONFIG, DEFAULT_ENTRY_CONFIG, DEFAULT_EXECUTION_CONFIG, DEFAULT_STOP_LOSS_CONFIG,
)
from src.data.historical_pipeline import build_standard_dataset
from src.strategies.context import MarketContext
from src.research.parameter_sweep import coordinate_sweep

sys.path.insert(0, str(ROOT / "scripts"))
from run_institutional_research import RESEARCH_RISK_CONFIG  # reuse the risk-lockout workaround

OUT_DIR = ROOT / "reports" / "edge_refinement"


def log(msg: str) -> None:
    print(msg, flush=True)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    dataset = build_standard_dataset(
        ROOT / "data/raw/EURUSD_M1_histdata.parquet", provider="histdata", symbol="EURUSD",
        timeframe="M1", source_tz="UTC", expected_interval="1min",
    )
    m1_full = dataset.data[["timestamp", "open", "high", "low", "close"]].copy()
    start = m1_full["timestamp"].iloc[0]
    m1 = m1_full[m1_full["timestamp"] < start + pd.Timedelta(days=91)].reset_index(drop=True)
    log(f"Slice: {len(m1)} candles, {m1['timestamp'].iloc[0]} -> {m1['timestamp'].iloc[-1]}")
    context = MarketContext(symbol="EURUSD", m1=m1)

    base_configs = {
        "S1": DEFAULT_S1_CONFIG, "S2": DEFAULT_S2_CONFIG, "S3": DEFAULT_S3_CONFIG,
        "S4": DEFAULT_S4_CONFIG, "S5": DEFAULT_S5_CONFIG, "tp_config": DEFAULT_TAKE_PROFIT_CONFIG,
        "entry_config": DEFAULT_ENTRY_CONFIG, "execution_config": DEFAULT_EXECUTION_CONFIG,
        "stop_config": DEFAULT_STOP_LOSS_CONFIG, "risk_config": RESEARCH_RISK_CONFIG,
    }

    results = {}

    def save_incremental() -> None:
        if not results:
            return
        rows = []
        for name, hdf in results.items():
            hdf = hdf.copy()
            hdf.insert(0, "sweep", name)
            rows.append(hdf)
        pd.concat(rows, ignore_index=True).to_csv(OUT_DIR / "entry_exit_refinement.csv", index=False)

    # --- Phase 5: Entry refinement ---
    for sid in ["S3", "S4"]:
        log(f"=== Phase 5: entry refinement, {sid} ===")
        latency = coordinate_sweep(
            "EURUSD", m1, context, base_configs,
            [("execution_config", "latency_candles", [0, 1, 2, 3])],
            strategy_filter=[sid],
        )
        results[f"{sid}_latency_candles"] = latency["history"]
        save_incremental()

        method = coordinate_sweep(
            "EURUSD", m1, context, base_configs,
            [("entry_config", "method", ["market", "confirmation_close", "ob_touch", "ob_proximal_edge"])],
            strategy_filter=[sid],
        )
        results[f"{sid}_entry_method"] = method["history"]
        save_incremental()

        fvg = coordinate_sweep(
            "EURUSD", m1, context, base_configs,
            [(sid, "require_fvg", [True, False])],
            strategy_filter=[sid],
        )
        results[f"{sid}_require_fvg"] = fvg["history"]
        save_incremental()

        fresh_ob = coordinate_sweep(
            "EURUSD", m1, context, base_configs,
            [(sid, "require_fresh_ob", [True, False])],
            strategy_filter=[sid],
        )
        results[f"{sid}_require_fresh_ob"] = fresh_ob["history"]
        save_incremental()

    # --- Phase 6: Exit refinement ---
    for sid in ["S3", "S4"]:
        log(f"=== Phase 6: exit refinement, {sid} ===")
        tp_method = coordinate_sweep(
            "EURUSD", m1, context, base_configs,
            [("tp_config", "method", ["fixed_rr", "previous_high_low", "liquidity_level", "next_bos_target"])],
            strategy_filter=[sid],
        )
        results[f"{sid}_tp_method"] = tp_method["history"]
        save_incremental()

        stop_method = coordinate_sweep(
            "EURUSD", m1, context, base_configs,
            [("stop_config", "method", ["ob_extreme", "m5_structural", "atr_multiple", "fixed_pips"])],
            strategy_filter=[sid],
        )
        results[f"{sid}_stop_method"] = stop_method["history"]
        save_incremental()

    all_rows = []
    for name, df in results.items():
        df = df.copy()
        df.insert(0, "sweep", name)
        all_rows.append(df)
    combined = pd.concat(all_rows, ignore_index=True)
    combined.to_csv(OUT_DIR / "entry_exit_refinement.csv", index=False)
    log(f"Wrote {OUT_DIR / 'entry_exit_refinement.csv'}")
    for name, df in results.items():
        log(f"\n=== {name} ===")
        log(df[[c for c in df.columns if c not in ("research_id",)]].to_string(index=False))


if __name__ == "__main__":
    main()
