"""
Task 12 Phase 14 -- First real Research Robustness Layer experiment:
the Sunday/Monday gap-reversion hypothesis on EURUSD.

Pipeline (matches docs/RESEARCH_ROBUSTNESS_FRAMEWORK.md exactly):
  1. Load EURUSD M1 data, build a strict TRAIN/VALIDATION/OOS split (OOS locked).
  2. Sample N bounded configurations, run each against TRAIN+VALIDATION only.
  3. Raw one-sample t-test p-values on each survivor's trade P&Ls.
  4. Bonferroni + Benjamini-Hochberg correction across the whole batch.
  5. Unlock OOS (explicit, logged reason) -- re-run BH-adjusted survivors on OOS ONLY.
  6. Monte Carlo (bootstrap + trade-order randomization) on OOS survivors.
  7. Parameter stability (neighbor perturbation) on OOS survivors.
  8. Cost/execution stress test on OOS survivors.
  9. Research Robustness Score per OOS survivor.
  10. Register the experiment, write CSV of the FULL search universe (incl. failures), write the markdown report.

Every number in the resulting report is real -- this script does not
manufacture a result. If nothing survives, the report says so explicitly
("NO ROBUST EDGE FOUND."), which this task's brief states is itself a
valid, successful research outcome.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from src.strategies.context import MarketContext
from src.research.robustness.data_split import build_research_dataset
from src.research.robustness.search_engine import sample_configurations, run_one_configuration, config_id
from src.research.robustness.multiple_testing import one_sample_t_test_pvalue, bonferroni_correction, benjamini_hochberg_correction, deflated_sharpe_ratio
from src.research.robustness.monte_carlo import run_bootstrap_resampling, run_trade_order_randomization
from src.research.robustness.parameter_stability import test_parameter_stability
from src.research.robustness.cost_stress import run_cost_stress_test, summarize_cost_stress
from src.research.robustness.robustness_score import compute_rrs, RRSInputs
from src.research.robustness.registry import register_experiment
from src.research.robustness.report import write_results_csv, write_results_parquet, render_experiment_report_markdown

PARAM_SPACE = {
    "gap_min_pct": [0.05, 0.10, 0.15, 0.20, 0.30, 0.50, 1.00],
    "gap_max_pct": [None, 1.0, 2.0],
    "gap_direction": ["up", "down", "both"],
    "require_choch": [True, False],
    "require_ob": [True, False],
    "ob_min_quality": [0.0, 0.3, 0.5, 0.7],
    "require_fvg": [True, False],
    "fvg_min_size_pct": [0.0, 0.02, 0.05],
    "fvg_retracement_pct_required": [0.0, 25.0, 50.0, 75.0],
    "fvg_must_be_unfilled": [True, False],
    "stop_reference": ["ob_extreme", "fixed_pips", "atr_multiple", "percentage"],
    "target_style": ["gap_fill_25", "gap_fill_50", "gap_fill_75", "gap_fill_100", "fixed_rr", "liquidity_level"],
    "risk_reward": [1.0, 1.5, 2.0, 2.5, 3.0],
    "session_filter": [None, ("london",), ("new_york",), ("london", "new_york")],
    "day_of_week_filter": [None, (0,), (0, 1)],
    "volatility_filter_atr_mult": [None, 0.75, 1.0, 1.5],
    "confidence_threshold": [0.0, 40.0, 60.0],
}

MIN_TRADES_FOR_SIGNIFICANCE = 10   # below this, a config's p-value is not computed/considered (too noisy)
MIN_TRADES_FOR_OOS_CANDIDATE = 5   # below this, an OOS result cannot be judged
BH_ALPHA = 0.05


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="EURUSD")
    parser.add_argument("--start", default="2023-01-01")
    parser.add_argument("--end", default="2023-07-03")
    parser.add_argument("--n-configs", type=int, default=500)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--data-path", default="data/processed/historical/EURUSD_M1.parquet")
    parser.add_argument("--results-dir", default="reports/robustness")
    args = parser.parse_args()

    t_start = time.time()
    print(f"Loading {args.data_path} [{args.start}, {args.end})...")
    full = pd.read_parquet(args.data_path)
    m1 = full[(full["timestamp"] >= args.start) & (full["timestamp"] < args.end)][
        ["timestamp", "open", "high", "low", "close"]
    ].reset_index(drop=True)
    print(f"Loaded {len(m1)} M1 candles.")

    dataset = build_research_dataset(args.symbol, m1, train_pct=0.6, validation_pct=0.2)
    print("Split:", dataset.date_ranges())

    train_validation = pd.concat([dataset.train, dataset.validation], ignore_index=True)
    print(f"Building context over train+validation ({len(train_validation)} candles)...")
    ctx_train_val = MarketContext(symbol=args.symbol, m1=train_validation)

    configs = sample_configurations(PARAM_SPACE, args.n_configs, seed=args.seed)
    print(f"Sampled {len(configs)} distinct configurations (target {args.n_configs}, seed={args.seed}).")

    print("Running search over train+validation (OOS remains locked)...")
    t0 = time.time()
    all_results = []
    for i, params in enumerate(configs):
        result = run_one_configuration(ctx_train_val, train_validation, params, args.symbol, period="train_validation")
        all_results.append(result)
        if (i + 1) % 50 == 0:
            print(f"  {i + 1}/{len(configs)} configs done ({time.time() - t0:.1f}s elapsed)")
    print(f"Search phase complete: {time.time() - t0:.1f}s for {len(configs)} configs.")

    n_failed = sum(1 for r in all_results if r.error is not None)
    print(f"Failed configurations (recorded, not dropped): {n_failed}")

    testable = [r for r in all_results if r.error is None and r.num_trades >= MIN_TRADES_FOR_SIGNIFICANCE]
    print(f"Configurations with >= {MIN_TRADES_FOR_SIGNIFICANCE} trades (eligible for significance testing): {len(testable)}")

    p_values = []
    for r in testable:
        pnls = r.raw_metrics.get("r_multiple_distribution", {}).get("values", [])
        # Fall back to per-trade average PnL proxy if R-multiples are unavailable for any trade.
        t_stat, p = one_sample_t_test_pvalue(pnls) if pnls else (0.0, 1.0)
        p_values.append(p)

    raw_significant = [p < 0.05 for p in p_values]
    bonferroni = bonferroni_correction(p_values, alpha=0.05) if p_values else {"reject": [], "n_rejected": 0, "corrected_alpha": None}
    bh = benjamini_hochberg_correction(p_values, alpha=BH_ALPHA) if p_values else {"reject": [], "n_rejected": 0}

    print(f"Raw p<0.05 (uncorrected): {sum(raw_significant)} of {len(testable)}")
    print(f"Bonferroni survivors: {bonferroni.get('n_rejected', 0)}")
    print(f"Benjamini-Hochberg survivors: {bh.get('n_rejected', 0)}")

    bh_survivors = [testable[i] for i, rej in enumerate(bh.get("reject", [])) if rej and testable[i].expectancy > 0]
    print(f"BH-adjusted + positive-expectancy survivors promoted to OOS: {len(bh_survivors)}")

    # --- Unlock OOS, explicitly, only now that selection is frozen -----
    dataset.unlock_out_of_sample(reason=f"configuration selection frozen after {len(configs)} train+validation configs; {len(bh_survivors)} promoted to OOS")
    oos_data = dataset.out_of_sample
    ctx_oos = MarketContext(symbol=args.symbol, m1=oos_data)
    print(f"OOS unlocked: {len(oos_data)} candles. Re-running {len(bh_survivors)} survivor(s) on OOS...")

    oos_results = []
    monte_carlo_summaries = {}
    stability_results = []
    cost_stress_summaries = {}
    rrs_by_config = {}

    for r in bh_survivors:
        oos_result = run_one_configuration(ctx_oos, oos_data, r.parameters, args.symbol, period="out_of_sample")
        oos_results.append(oos_result)
        cid = oos_result.config_id
        print(f"  OOS {cid}: trades={oos_result.num_trades}, expectancy={oos_result.expectancy}, PF={oos_result.profit_factor}")

        if oos_result.num_trades < MIN_TRADES_FOR_OOS_CANDIDATE:
            continue

        pnls = oos_result.raw_metrics.get("r_multiple_distribution", {}).get("values", [])
        if pnls:
            boot = run_bootstrap_resampling(pnls, n_simulations=5000, seed=args.seed)
            reorder = run_trade_order_randomization(pnls, n_simulations=5000, seed=args.seed)
            monte_carlo_summaries[cid] = {
                "bootstrap_prob_negative": boot.probability_of_negative_return,
                "reorder_prob_exceed_dd": reorder.probability_of_exceeding_observed_drawdown,
                "risk_of_ruin_pct": boot.risk_of_ruin_pct,
            }
        else:
            boot = None

        stability = test_parameter_stability(ctx_train_val, train_validation, r.parameters, args.symbol, min_neighbor_trades=3)
        stability_results.append(stability)

        cost_results = run_cost_stress_test(ctx_oos, oos_data, r.parameters, args.symbol)
        cost_summary = summarize_cost_stress(cost_results)
        cost_stress_summaries[cid] = cost_summary

        rrs_inputs = RRSInputs(
            oos_expectancy=oos_result.expectancy,
            monte_carlo_survival_rate=(1 - boot.probability_of_negative_return) if boot else None,
            parameter_stability_pass_rate=stability.stable_fraction,
            max_drawdown_pct=abs(oos_result.max_drawdown_pct) / 100.0 if oos_result.max_drawdown_pct else 0.0,
            bh_significant=True,
            num_oos_trades=oos_result.num_trades,
        )
        rrs_by_config[cid] = compute_rrs(rrs_inputs)

    dsr = None
    if testable:
        best_sharpe_result = max(testable, key=lambda r: r.sharpe_ratio)
        dsr = deflated_sharpe_ratio(best_sharpe_result.sharpe_ratio, n_trials=len(testable), n_observations=best_sharpe_result.num_trades)

    robust_survivors = [cid for cid, rrs in rrs_by_config.items() if rrs["rrs"] >= 70]
    if robust_survivors:
        final_verdict = (
            f"{len(robust_survivors)} configuration(s) survived every gate (BH-corrected significance, out-of-sample "
            f"positive expectancy, Monte Carlo, parameter stability, cost stress) with RRS >= 70: {robust_survivors}. "
            f"This is a RESEARCH CANDIDATE, not a validated trading edge -- forward/paper validation is still required "
            f"before any production consideration, per this framework's explicit governance rules."
        )
    elif rrs_by_config:
        final_verdict = (
            f"{len(rrs_by_config)} configuration(s) reached out-of-sample testing, but none reached RRS >= 70. "
            f"NO ROBUST EDGE FOUND. Highest RRS observed: {max(r['rrs'] for r in rrs_by_config.values())}."
        )
    else:
        final_verdict = "NO ROBUST EDGE FOUND. No configuration survived Benjamini-Hochberg correction with positive expectancy to even reach out-of-sample testing."

    print("\n" + final_verdict)

    results_dir = Path(args.results_dir)
    csv_path = write_results_csv(all_results, str(results_dir / "gap_experiment_all_configs.csv"))
    write_results_parquet(all_results, str(results_dir / "gap_experiment_all_configs.parquet"))

    record = register_experiment(
        experiment_name="gap_reversion_eurusd", dataset_identity=Path(args.data_path).name, symbols=[args.symbol],
        date_range=(args.start, args.end), parameter_search_space=PARAM_SPACE, n_configurations=len(configs),
        random_seed=args.seed, train_dates=dataset.date_ranges()["train"], validation_dates=dataset.date_ranges()["validation"],
        out_of_sample_dates=dataset.date_ranges()["out_of_sample"], statistical_correction_method=["bonferroni", "benjamini_hochberg", "deflated_sharpe_ratio_approx"],
        results_location=str(csv_path), notes=f"Task 12 Phase 14 first real experiment. Total runtime: {time.time() - t_start:.1f}s",
    )

    report_md = render_experiment_report_markdown(
        record.to_dict(), all_results=all_results, n_failed=n_failed,
        raw_significance={"reject": raw_significant}, bonferroni=bonferroni, bh=bh, dsr=dsr,
        oos_results=oos_results, monte_carlo_summaries=monte_carlo_summaries, stability_results=stability_results,
        cost_stress_summaries=cost_stress_summaries, rrs_by_config=rrs_by_config, final_verdict=final_verdict,
    )
    report_path = results_dir / f"{record.experiment_id}_report.md"
    report_path.write_text(report_md, encoding="utf-8")

    print(f"\nExperiment ID: {record.experiment_id}")
    print(f"Report: {report_path}")
    print(f"Results CSV: {csv_path}")
    print(f"Total runtime: {time.time() - t_start:.1f}s")


if __name__ == "__main__":
    main()
