"""
Task 7 real-market validation campaign orchestration.

This module runs the completed platform end to end on prepared historical
datasets. It deliberately does not optimize parameters or alter strategy
logic; its job is evidence gathering and export generation.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pandas as pd

from config.settings import DEFAULT_RISK_CONFIG
from src.backtest.engine import run_backtest
from src.backtest.metrics import compute_performance_metrics, trades_to_dataframe
from src.backtest.trade import TradeStatus
from src.data.historical_pipeline import build_standard_dataset, save_processed_dataset
from src.features.storage import save_feature_dataset
from src.research.analysis_utils import group_metrics
from src.research.confidence_analysis import (
    analyze_confidence_buckets,
    confidence_profitability_correlation,
)
from src.research.market_conditions import (
    classify_market_conditions,
    label_trades_with_conditions,
)
from src.research.portfolio_research import (
    analyze_portfolio_combinations,
    portfolio_correlation_summary,
)
from src.strategies.context import MarketContext
from src.strategies.runner import STRATEGY_MODULES, run_strategies
from src.utils.perf import ProgressReporter


DEFAULT_SYMBOLS = (
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "AUDUSD",
    "USDCHF",
    "USDCAD",
    "NZDUSD",
    "XAUUSD",
)


@dataclass(frozen=True)
class DatasetSpec:
    symbol: str
    path: Path
    provider: str = "dukascopy"
    source_tz: str | None = "UTC"
    timeframe: str = "M1"
    expected_interval: str = "1min"


@dataclass
class CampaignResult:
    datasets: list[dict] = field(default_factory=list)
    strategy_rankings: pd.DataFrame = field(default_factory=pd.DataFrame)
    portfolio_rankings: pd.DataFrame = field(default_factory=pd.DataFrame)
    market_condition_analysis: pd.DataFrame = field(default_factory=pd.DataFrame)
    confidence_validation: pd.DataFrame = field(default_factory=pd.DataFrame)
    trades: pd.DataFrame = field(default_factory=pd.DataFrame)
    warnings: list[str] = field(default_factory=list)


def discover_dataset_specs(
    raw_dir: str | Path,
    symbols: tuple[str, ...] = DEFAULT_SYMBOLS,
    provider: str = "dukascopy",
) -> list[DatasetSpec]:
    raw_dir = Path(raw_dir)
    specs = []
    for symbol in symbols:
        candidates = sorted(raw_dir.glob(f"{symbol}*.parquet")) + sorted(raw_dir.glob(f"{symbol}*.csv"))
        real_candidates = [p for p in candidates if "synthetic" not in p.name.lower()]
        if real_candidates:
            specs.append(DatasetSpec(symbol=symbol, path=real_candidates[0], provider=provider))
    return specs


def _estimate_dataset_candles(path: Path) -> int:
    """Task 7.4 Objective 5 -- cheap row-count peek used only to size the
    progress bar's total before any dataset is actually loaded. Reads
    parquet metadata (no data) when possible; falls back to a full read
    for other formats (acceptable since only .parquet is used for the
    large multi-year datasets this instrumentation targets)."""
    try:
        if path.suffix.lower() == ".parquet":
            import pyarrow.parquet as pq
            return pq.ParquetFile(path).metadata.num_rows
        return len(pd.read_csv(path, usecols=[0]))
    except Exception:
        return 0


def _closed(trades: list) -> list:
    return [t for t in trades if t.status == TradeStatus.CLOSED.value]


def _metric_value(metrics: dict, name: str) -> float:
    value = metrics.get(name, 0.0)
    if value == float("inf"):
        return 1e9
    return float(value or 0.0)


def _strategy_ranking_rows(trades_by_strategy: dict, starting_balance: float) -> list[dict]:
    rows = []
    for strategy_id, trades in sorted(trades_by_strategy.items()):
        metrics = compute_performance_metrics(trades, starting_balance)
        closed = metrics["signal_utilization"]["closed_trades"]
        drawdown = abs(_metric_value(metrics, "max_drawdown_pct"))
        score = (
            _metric_value(metrics, "expectancy")
            + _metric_value(metrics, "profit_factor")
            + _metric_value(metrics, "recovery_factor")
            - drawdown
        )
        rows.append(
            {
                "strategy": strategy_id,
                "trade_count": closed,
                "win_rate": metrics["win_rate"],
                "profit_factor": metrics["profit_factor"],
                "expectancy": metrics["expectancy"],
                "max_drawdown_pct": metrics["max_drawdown_pct"],
                "recovery_factor": metrics["recovery_factor"],
                "average_trade_duration": metrics["average_trade_duration_candles"],
                "mae": metrics["average_mae"],
                "mfe": metrics["average_mfe"],
                "sharpe_ratio": metrics["sharpe_ratio"],
                "sortino_ratio": metrics["sortino_ratio"],
                "calmar_ratio": metrics["recovery_factor"],
                "ranking_score": round(score, 4),
            }
        )
    return rows


def _market_condition_frames(trades: list, starting_balance: float, symbol: str) -> list[pd.DataFrame]:
    """One frame per condition dimension for a SINGLE symbol's trades.
    Callers append these across symbols into one combined list before
    concatenating -- the `symbol` column is what keeps otherwise-identical
    condition labels (e.g. two "trending" rows from EURUSD and GBPUSD)
    distinguishable after that concatenation; omitting it was a real bug
    caught during review (rows became silently ambiguous across symbols)."""
    frames = []
    keys = {
        "trend_state": lambda t: t.metadata.get("trend_state", "unknown"),
        "volatility_state": lambda t: t.metadata.get("volatility_state", "unknown"),
        "directional_bias": lambda t: t.metadata.get("directional_bias", "unknown"),
        # NOTE: must be a string, not bool -- `group_metrics`'s "group" column
        # (renamed to "condition" below) is concatenated across every
        # condition_type AND every symbol into one column. Every other
        # condition_type here already yields strings ("trending", "high",
        # "bull", "london", ...); a bare bool would make that column
        # mixed-type, which PyArrow refuses to write to Parquet at all
        # (confirmed during review: this crashed `_write_parquet` on the
        # very first non-trivial dataset with any gap day -- i.e. any real
        # multi-week FX dataset, since weekend gaps are routine).
        "gap_day": lambda t: "gap_day" if t.metadata.get("is_gap_day", False) else "normal_day",
        "session": lambda t: t.session or "unknown",
    }
    for name, fn in keys.items():
        frame = group_metrics(trades, fn, starting_balance)
        frame.insert(0, "condition_type", name)
        frame.insert(0, "symbol", symbol)
        frames.append(frame.rename(columns={"group": "condition"}))
    return frames


def _confidence_validation(trades: list) -> pd.DataFrame:
    corr = confidence_profitability_correlation(trades)
    closed = _closed(trades)
    if len(closed) >= 3:
        df = pd.DataFrame(
            {
                "confidence": [t.confidence_score for t in closed],
                "pnl": [t.realized_pnl for t in closed],
                "r_multiple": [t.r_multiple for t in closed],
            }
        )
        corr["pearson_confidence_vs_pnl"] = round(float(df["confidence"].corr(df["pnl"])), 4)
        corr["pearson_confidence_vs_r"] = round(float(df["confidence"].corr(df["r_multiple"])), 4)
    buckets = analyze_confidence_buckets(trades)
    for key, value in corr.items():
        buckets[key] = value
    return buckets


def _failure_analysis(trades: list) -> pd.DataFrame:
    losing = [t for t in _closed(trades) if t.realized_pnl < 0]
    rows = []
    for trade in losing:
        for reason in trade.reason_codes:
            rows.append(
                {
                    "strategy": trade.strategy_id,
                    "symbol": trade.symbol,
                    "failure_factor": str(reason),
                    "realized_pnl": trade.realized_pnl,
                    "r_multiple": trade.r_multiple,
                }
            )
    if not rows:
        return pd.DataFrame(columns=["strategy", "symbol", "failure_factor", "count", "avg_r"])
    df = pd.DataFrame(rows)
    return (
        df.groupby(["strategy", "symbol", "failure_factor"], as_index=False)
        .agg(count=("failure_factor", "size"), avg_r=("r_multiple", "mean"))
        .sort_values(["count", "avg_r"], ascending=[False, True])
        .reset_index(drop=True)
    )


def _write_dashboard(out_dir: Path, result: CampaignResult) -> None:
    html = f"""
<html>
<head><title>Forex SMC Quant Validation Dashboard</title></head>
<body>
<h1>Forex SMC Quant Validation Dashboard</h1>
<h2>Strategy Rankings</h2>
{result.strategy_rankings.to_html(index=False)}
<h2>Portfolio Rankings</h2>
{result.portfolio_rankings.to_html(index=False)}
<h2>Market Conditions</h2>
{result.market_condition_analysis.to_html(index=False)}
<h2>Confidence Validation</h2>
{result.confidence_validation.to_html(index=False)}
</body>
</html>
"""
    (out_dir / "research_dashboard.html").write_text(html, encoding="utf-8")


def _table_text(df: pd.DataFrame) -> str:
    if df.empty:
        return "_No rows generated._"
    return "```text\n" + df.to_string(index=False) + "\n```"


def _write_summary(out_dir: Path, result: CampaignResult, failure_analysis: pd.DataFrame) -> None:
    lines = [
        "# Real Market Validation Summary",
        "",
        "## Executive Summary",
        "",
        "This report is generated by the Task 7 validation campaign. It evaluates",
        "the completed SMC quant platform on available real historical datasets",
        "without parameter optimization or strategy redesign.",
        "",
        "## Datasets Used",
        "",
    ]
    if result.datasets:
        for dataset in result.datasets:
            lines.append(
                f"- {dataset['symbol']}: {dataset['candles']} candles, "
                f"{dataset['start']} to {dataset['end']}, provider={dataset['provider']}, "
                f"quality={dataset['quality_score']}"
            )
    else:
        lines.append("- No real historical datasets were available in the input directory.")

    lines.extend(["", "## Strategy Rankings", "", _table_text(result.strategy_rankings)])
    lines.extend(["", "## Portfolio Rankings", "", _table_text(result.portfolio_rankings)])
    lines.extend(["", "## Confidence Analysis", "", _table_text(result.confidence_validation)])
    lines.extend(["", "## Failure Analysis", "", _table_text(failure_analysis.head(25))])
    lines.extend(["", "## Recommendations", ""])
    lines.append("- Treat strategies with low trade count as inconclusive rather than rejected.")
    lines.append("- Reject or quarantine strategies with negative expectancy across symbols/regimes.")
    lines.append("- Recalibrate confidence only after this campaign establishes stable evidence.")
    (out_dir / "validation_summary.md").write_text("\n".join(lines), encoding="utf-8")


def _write_pdf(out_dir: Path, result: CampaignResult) -> None:
    from matplotlib.backends.backend_pdf import PdfPages
    import matplotlib.pyplot as plt

    pdf_path = out_dir / "validation_report.pdf"
    with PdfPages(pdf_path) as pdf:
        fig = plt.figure(figsize=(8.27, 11.69))
        fig.text(0.08, 0.94, "Forex SMC Quant Validation Report", fontsize=18, weight="bold")
        fig.text(0.08, 0.88, f"Datasets analysed: {len(result.datasets)}", fontsize=11)
        fig.text(0.08, 0.84, f"Trades generated: {len(result.trades)}", fontsize=11)
        if not result.strategy_rankings.empty:
            best = result.strategy_rankings.iloc[0].to_dict()
            fig.text(0.08, 0.78, f"Top strategy: {best.get('strategy')}", fontsize=11)
            fig.text(0.08, 0.74, f"Expectancy: {best.get('expectancy')}", fontsize=11)
            fig.text(0.08, 0.70, f"Profit factor: {best.get('profit_factor')}", fontsize=11)
        pdf.savefig(fig)
        plt.close(fig)


def _write_parquet(df: pd.DataFrame, path: Path, index: bool = False) -> None:
    # Delegates to the same JSON-encode-then-write helper every other
    # dataset in the project uses (src.features.storage.save_feature_dataset)
    # rather than re-implementing nested-column encoding here.
    save_feature_dataset(df, path, index=index)


def run_validation_campaign(
    dataset_specs: list[DatasetSpec],
    out_dir: str | Path,
    processed_dir: str | Path,
    starting_balance: float = DEFAULT_RISK_CONFIG.starting_balance,
    show_progress: bool = True,
) -> CampaignResult:
    out_dir = Path(out_dir)
    processed_dir = Path(processed_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    processed_dir.mkdir(parents=True, exist_ok=True)

    result = CampaignResult()
    all_trades = []
    trades_by_strategy: dict[str, list] = {strategy_id: [] for strategy_id in STRATEGY_MODULES}
    market_frames = []

    # Task 7.4 Objective 5: total_units is a rough candle-equivalent size
    # for the whole campaign (sum of each dataset's row count), used only
    # to compute %/ETA for the progress reporter below -- it has no effect
    # on what gets computed. Stage weights (LOAD/STRATEGIES/BACKTEST/
    # MARKET_CONDITIONS) are an approximate split of a dataset's "work"
    # across its four processing stages, based on the relative wall-time
    # shares observed in Task 7.4's profiling; they only affect how smooth
    # the progress bar looks, never correctness.
    total_units = sum(_estimate_dataset_candles(spec.path) for spec in dataset_specs) if show_progress else 0
    reporter = ProgressReporter(total_units, label="validation_campaign").start() if show_progress else None
    LOAD_W, STRAT_W, BACKTEST_W, COND_W = 0.05, 0.60, 0.25, 0.10

    for spec in dataset_specs:
        dataset = build_standard_dataset(
            spec.path,
            provider=spec.provider,
            symbol=spec.symbol,
            timeframe=spec.timeframe,
            source_tz=spec.source_tz,
            expected_interval=spec.expected_interval,
        )
        m1 = dataset.data[["timestamp", "open", "high", "low", "close"]].copy()
        if "volume" in dataset.data.columns:
            m1["volume"] = dataset.data["volume"]
        save_processed_dataset(dataset.data, processed_dir / f"{spec.symbol}_M1.parquet")

        dataset_units = len(m1)
        if reporter is not None:
            reporter.checkpoint(f"{spec.symbol}:load", int(dataset_units * LOAD_W))

        context = MarketContext(symbol=spec.symbol, m1=m1)

        strat_progress_cb = None
        if reporter is not None:
            per_strategy_units = int(dataset_units * STRAT_W / len(STRATEGY_MODULES))
            strat_progress_cb = lambda strategy_id: reporter.checkpoint(f"{spec.symbol}:strategies:{strategy_id}", per_strategy_units)
        signals = run_strategies(context, progress_cb=strat_progress_cb)

        backtest_progress_cb = None
        if reporter is not None:
            backtest_total_units = int(dataset_units * BACKTEST_W)
            last_reported = {"units": 0}

            def backtest_progress_cb(done, total, _bt_units=backtest_total_units, _last=last_reported):
                target = int(_bt_units * done / total) if total else _bt_units
                delta = target - _last["units"]
                _last["units"] = target
                if delta:
                    reporter.checkpoint(f"{spec.symbol}:backtest", delta)

        trades = run_backtest(signals, m1, context=context, progress_cb=backtest_progress_cb)
        conditions = classify_market_conditions(m1)
        label_trades_with_conditions(trades, conditions)
        if reporter is not None:
            reporter.checkpoint(f"{spec.symbol}:market_conditions", int(dataset_units * COND_W))

        for trade in trades:
            all_trades.append(trade)
            trades_by_strategy.setdefault(trade.strategy_id, []).append(trade)

        market_frames.extend(_market_condition_frames(trades, starting_balance, spec.symbol))
        result.datasets.append(
            {
                "symbol": spec.symbol,
                "provider": spec.provider,
                "candles": len(m1),
                "start": m1["timestamp"].iloc[0].isoformat() if not m1.empty else None,
                "end": m1["timestamp"].iloc[-1].isoformat() if not m1.empty else None,
                "quality_score": dataset.report.quality_score,
                "validation": asdict(dataset.report),
            }
        )

    if reporter is not None:
        reporter.stop()

    result.strategy_rankings = pd.DataFrame(_strategy_ranking_rows(trades_by_strategy, starting_balance))
    if not result.strategy_rankings.empty:
        result.strategy_rankings = result.strategy_rankings.sort_values(
            "ranking_score", ascending=False
        ).reset_index(drop=True)

    non_empty = {sid: trades for sid, trades in trades_by_strategy.items() if trades}
    result.portfolio_rankings = analyze_portfolio_combinations(non_empty, starting_balance) if non_empty else pd.DataFrame()
    result.market_condition_analysis = pd.concat(market_frames, ignore_index=True) if market_frames else pd.DataFrame()
    result.confidence_validation = _confidence_validation(all_trades)
    result.trades = trades_to_dataframe(all_trades) if all_trades else pd.DataFrame()

    failure_analysis = _failure_analysis(all_trades)
    result.strategy_rankings.to_csv(out_dir / "strategy_rankings.csv", index=False)
    result.portfolio_rankings.to_csv(out_dir / "portfolio_rankings.csv", index=False)
    _write_parquet(result.market_condition_analysis, out_dir / "market_condition_analysis.parquet")
    _write_parquet(result.confidence_validation, out_dir / "confidence_validation.parquet")
    _write_parquet(result.trades, out_dir / "trade_history.parquet")
    _write_parquet(failure_analysis, out_dir / "failure_analysis.parquet")
    correlations = portfolio_correlation_summary(non_empty) if non_empty else pd.DataFrame()
    _write_parquet(correlations, out_dir / "portfolio_correlations.parquet", index=True)
    (out_dir / "dataset_manifest.json").write_text(json.dumps(result.datasets, indent=2, default=str))
    _write_summary(out_dir, result, failure_analysis)
    _write_dashboard(out_dir, result)
    _write_pdf(out_dir, result)
    return result
