"""
Task 12 Phase 11 -- Reporting.

Turns the raw outputs of every other robustness module into the files
this task's brief names explicitly: a machine-readable CSV of every
tested configuration (never just the winner -- Phase 12's "never report
only the best configuration without showing the search universe"), and
a markdown summary covering rankings, raw-vs-adjusted significance, OOS
results, Monte Carlo, parameter stability, cost stress, and a clear
final verdict.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd


def write_results_csv(results: list, path: str) -> Path:
    """`results`: list of `ConfigResult`. Writes EVERY row, including
    failed/error'd configurations -- Phase 12 forbids silently dropping
    them."""
    df = pd.DataFrame([r.to_row() for r in results])
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(p, index=False)
    return p


def write_results_parquet(results: list, path: str) -> Path:
    df = pd.DataFrame([r.to_row() for r in results])
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(p, index=False)
    return p


def render_experiment_report_markdown(
    experiment_record: dict, all_results: list, n_failed: int,
    raw_significance: dict, bonferroni: dict, bh: dict, dsr,
    oos_results: list, monte_carlo_summaries: dict, stability_results: list,
    cost_stress_summaries: dict, rrs_by_config: dict, final_verdict: str,
) -> str:
    lines = [
        f"# Gap Reversion Research Experiment -- {experiment_record['experiment_id']}",
        "",
        f"Run: {experiment_record['timestamp']} | git commit: `{experiment_record['git_commit'][:12]}`",
        f"Dataset: {experiment_record['dataset_identity']} | Symbols: {', '.join(experiment_record['symbols'])}",
        f"Date range: {experiment_record['date_range']}",
        f"Train: {experiment_record['train_dates']} | Validation: {experiment_record['validation_dates']} | "
        f"Out-of-sample: {experiment_record['out_of_sample_dates']}",
        f"Configurations tested: {experiment_record['n_configurations']} (seed={experiment_record['random_seed']})",
        f"Statistical correction: {', '.join(experiment_record['statistical_correction_method'])}",
        "",
        "## Search universe summary",
        f"- Total configurations attempted: {len(all_results)}",
        f"- Failed/errored (recorded, not dropped): {n_failed}",
        f"- Configurations with >= 1 closed trade: {sum(1 for r in all_results if r.num_trades > 0)}",
        "",
        "## Raw vs. adjusted significance",
        f"- Raw candidates with p < 0.05 (uncorrected): {sum(1 for r in raw_significance.get('reject', []) if r)}",
        f"- Bonferroni-corrected survivors (alpha={bonferroni.get('corrected_alpha', 'n/a')}): {bonferroni.get('n_rejected', 0)}",
        f"- Benjamini-Hochberg FDR survivors: {bh.get('n_rejected', 0)}",
    ]
    if dsr is not None:
        lines.append(f"- Deflated Sharpe Ratio (best config): {dsr.deflated_sharpe_ratio} (observed Sharpe {dsr.observed_sharpe}, expected max under null {dsr.expected_max_sharpe_under_null} over {dsr.n_trials} trials)")

    lines += ["", "## Out-of-sample results (the only numbers that count for a final claim)"]
    if not oos_results:
        lines.append("- No configuration was promoted to out-of-sample testing.")
    for r in oos_results:
        lines.append(f"- `{r.config_id}`: {r.num_trades} OOS trades, expectancy={r.expectancy}, profit_factor={r.profit_factor}, max_dd={r.max_drawdown_pct}%")

    lines += ["", "## Monte Carlo robustness (bootstrap + trade-order randomization)"]
    for cid, summary in monte_carlo_summaries.items():
        lines.append(f"- `{cid}`: P(negative return, bootstrap)={summary.get('bootstrap_prob_negative')}, "
                     f"P(exceeding observed drawdown, order-randomization)={summary.get('reorder_prob_exceed_dd')}, "
                     f"risk of ruin={summary.get('risk_of_ruin_pct')}%")

    lines += ["", "## Parameter stability"]
    for s in stability_results:
        lines.append(f"- `{s.config_id}`: verdict={s.verdict}, stable_fraction={s.stable_fraction}, "
                     f"collapsed_fields={s.collapsed_fields or 'none'}")

    lines += ["", "## Cost/execution stress"]
    for cid, summary in cost_stress_summaries.items():
        lines.append(f"- `{cid}`: survives adverse execution = {summary.get('survives_adverse_execution')}, "
                     f"degradation under adverse execution = {summary.get('degradation_pct')}%")

    lines += ["", "## Research Robustness Score (RRS) -- research-only, does NOT feed IOS/ITQS"]
    for cid, rrs in sorted(rrs_by_config.items(), key=lambda kv: -kv[1]["rrs"]):
        lines.append(f"- `{cid}`: RRS={rrs['rrs']} -- {rrs['interpretation']}")

    lines += ["", "## Final verdict", "", final_verdict]
    return "\n".join(lines)
