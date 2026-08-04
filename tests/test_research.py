"""
Task 5 Research & Optimisation Laboratory tests.

Most tests construct `Trade` objects directly (fast, deterministic) to
test each analysis module in isolation. A handful of integration-level
tests run the full experiment pipeline on a small (3-day) synthetic
dataset for properties that can only be verified end-to-end
(reproducibility, no-look-ahead, parameter sweeps, export correctness).
"""

import json

import pandas as pd
import pytest

from config.settings import S3Config, ManagementConfig, TakeProfitConfig
from src.backtest.trade import Trade, TradeStatus
from src.strategies.context import MarketContext
from src.research.experiment import run_experiment
from src.research.parameter_sweep import grid_sweep, coordinate_sweep
from src.research.market_conditions import classify_market_conditions, label_trades_with_conditions
from src.research.symbol_analysis import analyze_symbols, rank_symbols
from src.research.session_analysis import analyze_sessions, analyze_session_overlaps
from src.research.confidence_analysis import analyze_confidence_buckets, confidence_profitability_correlation
from src.research.filter_analysis import has_fvg, has_engulfing, evaluate_filter, compare_filters
from src.research.portfolio_research import generate_combinations, analyze_portfolio_combinations, best_portfolio
from src.research.walkforward_research import generate_rolling_windows, evaluate_rolling_windows, summarize_stability
from src.research.sensitivity import find_best_value, detect_diminishing_returns
from src.research.strategy_analysis import analyze_strategies, calmar_ratio
from src.research.reporting import save_research_datasets, export_experiment, experiments_to_summary_df
from src.research.visualizations import (
    equity_curves_chart, drawdown_comparison_chart, parameter_heatmap, sensitivity_curve_chart,
    confidence_distribution_chart, strategy_comparison_chart, portfolio_comparison_chart,
    trade_distribution_chart, session_performance_chart, symbol_performance_chart,
    correlation_matrix_chart, monthly_returns_chart, expectancy_distribution_chart, build_research_dashboard,
)
from src.backtest.portfolio import compare_strategies
from tests.helpers import make_multi_day_m1


def _trade(pnl, ts, strategy_id="S3", symbol="TEST", session="london", confidence=90.0, reason_codes=None, entry_ts=None):
    ts = pd.Timestamp(ts, tz="UTC")
    return Trade(
        trade_id=f"T_{strategy_id}_{ts}", signal_id="S", strategy_id=strategy_id, symbol=symbol, timeframe="M1", direction="bullish",
        signal_timestamp=ts, confidence_score=confidence, reason_codes=reason_codes or [strategy_id],
        confluence_snapshot={}, entry_method="market", stop_method="fixed_pips", take_profit_method="fixed_rr",
        status=TradeStatus.CLOSED.value, entry_price=1.10, exit_price=1.10 + pnl / 100_000,
        entry_timestamp=entry_ts or ts, exit_timestamp=ts, exit_reason="TAKE_PROFIT" if pnl > 0 else "STOP_LOSS",
        realized_pnl=pnl, r_multiple=pnl / 100.0, duration_candles=10, mae=abs(min(pnl, 0)), mfe=max(pnl, 0),
        session=session,
    )


@pytest.fixture(scope="module")
def small_m1():
    return make_multi_day_m1(num_days=3, seed=11)


@pytest.fixture(scope="module")
def small_context(small_m1):
    return MarketContext(symbol="TEST", m1=small_m1)


# ---------------------------------------------------------------------------
# Experiment reproducibility + no-look-ahead (integration-level)
# ---------------------------------------------------------------------------


def test_experiment_reproducibility(small_m1, small_context):
    exp1 = run_experiment("baseline", "TEST", small_m1, context=small_context)
    exp2 = run_experiment("baseline", "TEST", small_m1, context=small_context)
    assert exp1.research_id == exp2.research_id
    assert exp1.results["metrics"] == exp2.results["metrics"]
    assert exp1.results["num_trades"] == exp2.results["num_trades"]


def test_experiment_different_parameters_get_different_research_id(small_m1, small_context):
    exp1 = run_experiment("t1", "TEST", small_m1, context=small_context, parameter_set={"x": 1})
    exp2 = run_experiment("t1", "TEST", small_m1, context=small_context, parameter_set={"x": 2})
    assert exp1.research_id != exp2.research_id


def test_experiment_no_lookahead_via_truncated_dataset(small_m1):
    """An experiment run on a truncated dataset must produce trades
    identical (for those entered before the cutoff) to the corresponding
    prefix of an experiment run on the full dataset -- built directly on
    Task 3/4's own no-look-ahead guarantees, verified again at the
    experiment-wrapper level to ensure this module adds no new leakage.

    Uses `LiquidityConfig(equal_level_tolerance=0.0)` for the same reason
    as `tests/test_strategies.py::test_no_lookahead_bias_...`: with the
    default tolerance, Task 2's batch liquidity-clustering sorts swings by
    PRICE rather than time and can merge a later swing into an earlier
    level's cluster, shifting that level's price using information not
    yet available at the earlier timestamp -- a known Task 2 batch-engine
    property (see docs/STRATEGY_ENGINE.md), not something this module
    introduces. Disabling clustering isolates the property actually under
    test here: whether the EXPERIMENT WRAPPER adds any additional
    look-ahead beyond what the underlying engines already have documented.
    """
    from config.settings import LiquidityConfig

    cutoff = small_m1["timestamp"].iloc[len(small_m1) // 2]
    truncated = small_m1[small_m1["timestamp"] <= cutoff].reset_index(drop=True)

    full_context = MarketContext(symbol="TEST", m1=small_m1, liquidity_config=LiquidityConfig(equal_level_tolerance=0.0))
    truncated_context = MarketContext(symbol="TEST", m1=truncated, liquidity_config=LiquidityConfig(equal_level_tolerance=0.0))

    full_exp = run_experiment("full", "TEST", small_m1, context=full_context)
    truncated_exp = run_experiment("truncated", "TEST", truncated, context=truncated_context)

    full_trades_before_cutoff = {
        (t.strategy_id, t.entry_timestamp, t.direction)
        for t in full_exp.results["trades"] if t.status == TradeStatus.CLOSED.value and t.entry_timestamp <= cutoff
    }
    truncated_trades = {
        (t.strategy_id, t.entry_timestamp, t.direction)
        for t in truncated_exp.results["trades"] if t.status == TradeStatus.CLOSED.value
    }
    assert truncated_trades == full_trades_before_cutoff


# ---------------------------------------------------------------------------
# Parameter sweeps (grid + coordinate)
# ---------------------------------------------------------------------------


def test_grid_sweep_runs_every_combination(small_m1, small_context):
    configs = {"S3": S3Config(), "management_config": ManagementConfig()}
    df = grid_sweep("TEST", small_m1, small_context, configs, [("management_config", "max_trade_duration_candles", [50, 100])])
    assert len(df) == 2
    assert set(df["management_config.max_trade_duration_candles"]) == {50, 100}


def test_grid_sweep_deterministic_repeatability(small_m1, small_context):
    configs = {"S3": S3Config(), "management_config": ManagementConfig()}
    df1 = grid_sweep("TEST", small_m1, small_context, configs, [("management_config", "max_trade_duration_candles", [50, 100])])
    df2 = grid_sweep("TEST", small_m1, small_context, configs, [("management_config", "max_trade_duration_candles", [50, 100])])
    pd.testing.assert_frame_equal(
        df1.drop(columns=["research_id"]).reset_index(drop=True),
        df2.drop(columns=["research_id"]).reset_index(drop=True),
    )


def test_coordinate_sweep_picks_exactly_one_winner_per_parameter(small_m1, small_context):
    configs = {"S3": S3Config(), "management_config": ManagementConfig(), "tp_config": TakeProfitConfig()}
    result = coordinate_sweep(
        "TEST", small_m1, small_context, configs,
        [("management_config", "max_trade_duration_candles", [50, 100]), ("tp_config", "risk_reward", [1.5, 2.0])],
    )
    history = result["history"]
    for field in ["max_trade_duration_candles", "risk_reward"]:
        subset = history[history.field == field]
        assert subset["chosen"].sum() == 1
    assert "best_configs" in result and "management_config" in result["best_configs"]


# ---------------------------------------------------------------------------
# Market conditions: causal (no look-ahead) classification
# ---------------------------------------------------------------------------


def test_market_conditions_are_causal_not_lookahead(small_m1):
    full = classify_market_conditions(small_m1, trend_window=20, vol_window=20, bull_bear_window=50)
    cutoff_idx = len(small_m1) // 2
    truncated = classify_market_conditions(small_m1.iloc[:cutoff_idx].reset_index(drop=True), trend_window=20, vol_window=20, bull_bear_window=50)

    # every row up to the cutoff must classify identically regardless of
    # whether MORE rows exist after it in the input
    check_idx = cutoff_idx - 1
    assert full.iloc[check_idx]["trend_state"] == truncated.iloc[check_idx]["trend_state"]
    assert full.iloc[check_idx]["volatility_state"] == truncated.iloc[check_idx]["volatility_state"]
    assert full.iloc[check_idx]["directional_bias"] == truncated.iloc[check_idx]["directional_bias"]


def test_label_trades_with_conditions_uses_asof_not_future_row():
    conditions = pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01", periods=3, freq="1h", tz="UTC"),
        "trend_state": ["ranging", "trending", "ranging"],
        "volatility_state": ["low", "low", "high"],
        "directional_bias": ["neutral", "bull", "bear"],
        "is_gap_day": [False, False, False],
        "is_news_day": [False, False, False],
    })
    trade = _trade(50, "2024-01-01 01:30:00", entry_ts=pd.Timestamp("2024-01-01 01:30:00", tz="UTC"))
    label_trades_with_conditions([trade], conditions)
    assert trade.metadata["trend_state"] == "trending"  # the 01:00 row (asof), not the 02:00 row


# ---------------------------------------------------------------------------
# Symbol / Session / Confidence / Filter analysis
# ---------------------------------------------------------------------------


def test_symbol_analysis_and_ranking():
    trades = [_trade(100, "2024-01-01", symbol="EURUSD"), _trade(-40, "2024-01-02", symbol="EURUSD"),
              _trade(200, "2024-01-01", symbol="GBPUSD")]
    df = analyze_symbols(trades)
    assert set(df["symbol"]) == {"EURUSD", "GBPUSD"}
    ranked = rank_symbols(trades)
    assert ranked.iloc[0]["symbol"] == "GBPUSD"  # higher expectancy


def test_session_analysis_and_overlaps(small_context):
    trades = [_trade(100, "2024-01-01 09:00:00", session="london"), _trade(-40, "2024-01-01 10:00:00", session="new_york")]
    df = analyze_sessions(trades)
    assert set(df["session"]) == {"london", "new_york"}
    overlap_df = analyze_session_overlaps(trades, small_context)
    assert "session_combination" in overlap_df.columns


def test_confidence_bucket_analysis_and_correlation():
    trades = [_trade(100, "2024-01-01", confidence=95), _trade(-50, "2024-01-02", confidence=55),
              _trade(80, "2024-01-03", confidence=92)]
    df = analyze_confidence_buckets(trades)
    assert set(df["confidence_range"]) == {"90-100", "50-60"}
    corr = confidence_profitability_correlation(trades)
    assert corr["n"] == 3


def test_filter_analysis_with_without_and_verdict():
    trades = [
        _trade(100, "2024-01-01", reason_codes=["S3", "FVGAligned"]),
        _trade(90, "2024-01-02", reason_codes=["S3", "FVGAligned"]),
        _trade(-30, "2024-01-03", reason_codes=["S3"]),
        _trade(-20, "2024-01-04", reason_codes=["S3"]),
    ]
    table = evaluate_filter(trades, has_fvg, "HasFVG")
    with_row = table[table.group == "With"].iloc[0]
    without_row = table[table.group == "Without"].iloc[0]
    assert with_row["num_trades"] == 2
    assert without_row["num_trades"] == 2
    assert with_row["expectancy"] > without_row["expectancy"]

    comparison = compare_filters(trades, {"HasFVG": has_fvg, "HasEngulfing": has_engulfing})
    # NOTE: index with comparison["filter"], not comparison.filter -- the
    # latter resolves to DataFrame.filter() (a real pandas method), not
    # the "filter" column, since attribute access checks methods first.
    assert comparison[comparison["filter"] == "HasFVG"].iloc[0]["verdict"] == "improves"


# ---------------------------------------------------------------------------
# Portfolio research
# ---------------------------------------------------------------------------


def test_generate_combinations_covers_singles_pairs_triples_and_all():
    combos = generate_combinations(["S1", "S2", "S3", "S4", "S5"])
    sizes = sorted({len(c) for c in combos})
    assert sizes == [1, 2, 3, 5]
    assert ("S1",) in combos
    assert ("S1", "S2") in combos
    assert ("S1", "S2", "S3") in combos
    assert ("S1", "S2", "S3", "S4", "S5") in combos


def test_portfolio_combination_analysis_and_best():
    by_strategy = {
        "S1": [_trade(100, "2024-01-01", strategy_id="S1"), _trade(-30, "2024-01-03", strategy_id="S1")],
        "S2": [_trade(50, "2024-01-02", strategy_id="S2")],
    }
    df = analyze_portfolio_combinations(by_strategy)
    assert set(df["combination"]) == {"S1", "S2", "S1+S2"}
    best = best_portfolio(df)
    assert best["combination"] in {"S1", "S2", "S1+S2"}


# ---------------------------------------------------------------------------
# Walk-forward research
# ---------------------------------------------------------------------------


def test_rolling_windows_generation_and_evaluation(small_m1):
    windows = generate_rolling_windows(small_m1, test_days=1, step_days=1, train_days=0)
    assert len(windows) > 0
    trades = [_trade(100, "2024-01-01 06:00:00"), _trade(-50, "2024-01-02 06:00:00")]
    rolling = evaluate_rolling_windows(trades, windows)
    assert "expectancy" in rolling.columns
    stability = summarize_stability(rolling)
    assert stability["num_windows"] == len(windows)


# ---------------------------------------------------------------------------
# Sensitivity analysis
# ---------------------------------------------------------------------------


def test_find_best_value_and_diminishing_returns():
    curve = pd.DataFrame({"threshold": [50, 60, 70, 80], "expectancy": [10, 20, 25, 25.1], "num_trades": [40, 30, 20, 10]})
    best = find_best_value(curve, "threshold", metric="expectancy")
    assert best["value"] == 80
    diminishing = detect_diminishing_returns(curve, "threshold", metric="expectancy", tolerance=0.05)
    assert diminishing["has_data"] is True
    assert diminishing["still_improving"] is False  # last delta (25.1-25=0.1) is well under 5% tolerance of 25


# ---------------------------------------------------------------------------
# Strategy analysis (Calmar + full metric suite)
# ---------------------------------------------------------------------------


def test_analyze_strategies_includes_calmar_and_core_metrics():
    by_strategy = {"S1": [_trade(100, "2024-01-01", strategy_id="S1"), _trade(-40, "2024-01-05", strategy_id="S1")]}
    df = analyze_strategies(by_strategy, starting_balance=10_000)
    row = df.iloc[0]
    for col in ("expectancy", "profit_factor", "win_rate", "average_winner", "average_loser",
                "max_drawdown_pct", "r_multiple_mean", "average_mae", "average_mfe",
                "recovery_factor", "sharpe_ratio", "sortino_ratio", "calmar_ratio"):
        assert col in row


def test_calmar_ratio_zero_drawdown_all_wins():
    trades = [_trade(100, "2024-01-01", strategy_id="S1"), _trade(50, "2024-01-02", strategy_id="S1")]
    assert calmar_ratio(trades, 10_000) == float("inf")


# ---------------------------------------------------------------------------
# Reporting / exports
# ---------------------------------------------------------------------------


def test_save_research_datasets_writes_all_seven_files(tmp_path, small_m1, small_context):
    exp = run_experiment("baseline", "TEST", small_m1, context=small_context)
    save_research_datasets(tmp_path, [exp])
    expected = [
        "research_summary.parquet", "experiment_results.parquet", "parameter_analysis.parquet",
        "portfolio_analysis.parquet", "confidence_analysis.parquet", "filter_analysis.parquet",
        "walkforward_results.parquet",
    ]
    for name in expected:
        assert (tmp_path / name).exists()
    summary = pd.read_parquet(tmp_path / "research_summary.parquet")
    assert summary.iloc[0]["research_id"] == exp.research_id


def test_export_experiment_all_formats_readable(tmp_path, small_m1, small_context):
    exp = run_experiment("baseline", "TEST", small_m1, context=small_context, notes="test notes")
    written = export_experiment(exp, tmp_path)
    assert set(written.keys()) == {"csv", "parquet", "json", "markdown", "html"}

    csv_df = pd.read_csv(written["csv"])
    assert csv_df.iloc[0]["research_id"] == exp.research_id

    payload = json.loads(open(written["json"]).read())
    assert payload["research_id"] == exp.research_id

    md_text = open(written["markdown"]).read()
    assert "test notes" in md_text

    html_text = open(written["html"]).read()
    assert exp.research_id in html_text


# ---------------------------------------------------------------------------
# Visualisations: every chart type generates without error
# ---------------------------------------------------------------------------


def test_all_visualization_functions_generate_valid_figures():
    trades = [_trade(100, "2024-01-01", strategy_id="S1", symbol="EURUSD", session="london"),
              _trade(-40, "2024-01-02", strategy_id="S2", symbol="EURUSD", session="new_york")]
    by_strategy = {"S1": [trades[0]], "S2": [trades[1]]}
    trades_by_label = {"S1": [trades[0]], "S2": [trades[1]]}

    comparison = compare_strategies(by_strategy, 10_000)
    portfolio_df = analyze_portfolio_combinations(by_strategy)
    session_df = analyze_sessions(trades)
    symbol_df = analyze_symbols(trades)
    from src.research.portfolio_research import portfolio_correlation_summary
    correlation = portfolio_correlation_summary(by_strategy)
    sweep_df = pd.DataFrame({"a": [1, 1, 2, 2], "b": [1, 2, 1, 2], "expectancy": [1, 2, 3, 4]})
    response_curve = pd.DataFrame({"threshold": [50, 60, 70], "expectancy": [1, 2, 3], "profit_factor": [1, 1.2, 1.4], "num_trades": [10, 8, 5]})

    figures = [
        equity_curves_chart(trades_by_label),
        drawdown_comparison_chart(trades_by_label),
        parameter_heatmap(sweep_df, "a", "b"),
        sensitivity_curve_chart(response_curve, "threshold"),
        confidence_distribution_chart(trades),
        strategy_comparison_chart(comparison),
        portfolio_comparison_chart(portfolio_df),
        trade_distribution_chart(trades),
        session_performance_chart(session_df),
        symbol_performance_chart(symbol_df),
        correlation_matrix_chart(correlation),
        monthly_returns_chart(trades),
        expectancy_distribution_chart(session_df, "session"),
        build_research_dashboard(trades, comparison, portfolio_df, session_df, symbol_df, correlation),
    ]
    for fig in figures:
        assert fig is not None
        assert hasattr(fig, "data")
