"""
Research Reporting: writes the seven required research datasets
(research_summary, experiment_results, parameter_analysis,
portfolio_analysis, confidence_analysis, filter_analysis,
walkforward_results, each as .parquet), plus independent per-experiment
exports in CSV / Parquet / JSON / Markdown / HTML.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from src.research.experiment import Experiment


def _json_default(obj):
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return str(obj)


def experiments_to_summary_df(experiments: list) -> pd.DataFrame:
    rows = []
    for e in experiments:
        m = e.results.get("metrics", {})
        rows.append({
            "research_id": e.research_id, "experiment_name": e.experiment_name,
            "timestamp": e.timestamp, "dataset": e.dataset, "strategy": json.dumps(e.strategy, default=_json_default),
            "num_signals": e.results.get("num_signals", 0), "num_trades": e.results.get("num_trades", 0),
            "net_profit": m.get("net_profit"), "win_rate": m.get("win_rate"),
            "profit_factor": m.get("profit_factor"), "expectancy": m.get("expectancy"),
            "max_drawdown_pct": m.get("max_drawdown_pct"), "sharpe_ratio": m.get("sharpe_ratio"),
            "notes": e.notes,
        })
    return pd.DataFrame(rows)


def experiments_to_full_df(experiments: list) -> pd.DataFrame:
    rows = []
    for e in experiments:
        results_copy = {k: v for k, v in e.results.items() if k != "trades"}  # Trade objects aren't parquet-serializable
        rows.append({
            "research_id": e.research_id, "experiment_name": e.experiment_name, "timestamp": e.timestamp,
            "dataset": e.dataset, "strategy": json.dumps(e.strategy, default=_json_default),
            "configuration": json.dumps(e.configuration, default=_json_default),
            "parameter_set": json.dumps(e.parameter_set, default=_json_default),
            "results": json.dumps(results_copy, default=_json_default),
            "notes": e.notes,
        })
    return pd.DataFrame(rows)


def save_research_datasets(
    out_dir: str | Path,
    experiments: list,
    parameter_analysis: pd.DataFrame | None = None,
    portfolio_analysis: pd.DataFrame | None = None,
    confidence_analysis: pd.DataFrame | None = None,
    filter_analysis: pd.DataFrame | None = None,
    walkforward_results: pd.DataFrame | None = None,
) -> None:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    experiments_to_summary_df(experiments).to_parquet(out_dir / "research_summary.parquet", index=False)
    experiments_to_full_df(experiments).to_parquet(out_dir / "experiment_results.parquet", index=False)

    empty = pd.DataFrame()
    (parameter_analysis if parameter_analysis is not None else empty).to_parquet(out_dir / "parameter_analysis.parquet", index=False)
    (portfolio_analysis if portfolio_analysis is not None else empty).to_parquet(out_dir / "portfolio_analysis.parquet", index=False)
    (confidence_analysis if confidence_analysis is not None else empty).to_parquet(out_dir / "confidence_analysis.parquet", index=False)
    (filter_analysis if filter_analysis is not None else empty).to_parquet(out_dir / "filter_analysis.parquet", index=False)
    (walkforward_results if walkforward_results is not None else empty).to_parquet(out_dir / "walkforward_results.parquet", index=False)


def _experiment_markdown(experiment: Experiment) -> str:
    m = experiment.results.get("metrics", {})
    lines = [
        f"# Experiment: {experiment.experiment_name}", "",
        f"- **Research ID**: `{experiment.research_id}`",
        f"- **Timestamp**: {experiment.timestamp}",
        f"- **Dataset**: {experiment.dataset}",
        f"- **Strategy**: {experiment.strategy}",
        f"- **Parameter set**: {experiment.parameter_set}",
        "",
        "## Results", "",
        f"- Signals: {experiment.results.get('num_signals')}",
        f"- Trades: {experiment.results.get('num_trades')}",
        f"- Net Profit: {m.get('net_profit')}",
        f"- Win Rate: {m.get('win_rate')}",
        f"- Profit Factor: {m.get('profit_factor')}",
        f"- Expectancy: {m.get('expectancy')}",
        f"- Max Drawdown %: {m.get('max_drawdown_pct')}",
        f"- Sharpe Ratio: {m.get('sharpe_ratio')}",
        "",
        "## Notes", "", experiment.notes or "_none_",
    ]
    return "\n".join(lines)


def export_experiment(experiment: Experiment, out_dir: str | Path, formats: tuple = ("csv", "parquet", "json", "markdown", "html")) -> dict:
    """Writes the given experiment independently in every requested
    format. Returns {format: path}."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    base = out_dir / experiment.research_id
    written = {}

    summary_df = experiments_to_summary_df([experiment])

    if "csv" in formats:
        path = base.with_suffix(".csv")
        summary_df.to_csv(path, index=False)
        written["csv"] = str(path)

    if "parquet" in formats:
        path = base.with_suffix(".parquet")
        experiments_to_full_df([experiment]).to_parquet(path, index=False)
        written["parquet"] = str(path)

    if "json" in formats:
        path = base.with_suffix(".json")
        payload = dict(experiment.to_dict())
        payload["results"] = {k: v for k, v in experiment.results.items() if k != "trades"}
        path.write_text(json.dumps(payload, indent=2, default=_json_default))
        written["json"] = str(path)

    if "markdown" in formats:
        path = base.with_suffix(".md")
        path.write_text(_experiment_markdown(experiment))
        written["markdown"] = str(path)

    if "html" in formats:
        path = base.with_suffix(".html")
        html = (
            f"<html><head><title>{experiment.experiment_name}</title></head><body>"
            f"<h1>{experiment.experiment_name}</h1>"
            f"<p><b>Research ID:</b> {experiment.research_id}</p>"
            f"<p><b>Dataset:</b> {experiment.dataset}</p>"
            f"<p><b>Parameter set:</b> {experiment.parameter_set}</p>"
            f"{summary_df.to_html(index=False)}"
            f"</body></html>"
        )
        path.write_text(html)
        written["html"] = str(path)

    return written
