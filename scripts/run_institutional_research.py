"""
Task 8 — Institutional Strategy Research & Edge Discovery.

Orchestrates the existing Task 5 research lab (src/research/*) plus the
new institutional_edge.py additions (failure categorization, IES) across
every symbol with available real historical data, answering:
"Which strategies deserve real capital?"

Three tiers of analysis, by data-depth requirement (documented explicitly
because not every symbol has equal history -- see the note on data scope
in docs/INSTITUTIONAL_RESEARCH_REPORT.md):

  Tier 1 (every available symbol): five strategies run INDEPENDENTLY
    (isolated risk tracker each, matching the task brief's "run each
    independently, then run combinations"), portfolio combinations,
    correlation, confidence validation, market regime analysis, session
    analysis, failure analysis.

  Tier 2 (EURUSD only -- the one symbol with full 6.5-year depth):
    year-by-year and month-by-month robustness, rolling 6-month and
    12-month walk-forward window stability. These require multi-year
    history to be meaningful; the other symbols' ~6-month real-data
    windows cannot support this tier (documented, not silently skipped).

  Tier 3 (EURUSD, 1-year slice): parameter robustness sweeps (R:R, gap
    size, CHoCH confirmation timeframe, OB-freshness requirement, session
    filter). Run on a 1-year slice rather than the full 6.5-year dataset
    because each sweep candidate requires a full strategies+backtest
    re-run (`run_experiment` regenerates all 5 strategies' signals per
    call by design -- see src/research/experiment.py); at full-dataset
    cost (tens of minutes per experiment) a 25-candidate sweep would take
    many hours. A 1-year slice keeps this tractable while still being
    real market data, not synthetic.
"""

from __future__ import annotations

import pickle
import sys
import time
from dataclasses import replace
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import (
    DEFAULT_S1_CONFIG, DEFAULT_S2_CONFIG, DEFAULT_S3_CONFIG, DEFAULT_S4_CONFIG, DEFAULT_S5_CONFIG,
    DEFAULT_TAKE_PROFIT_CONFIG, DEFAULT_RISK_CONFIG,
)
from src.data.historical_pipeline import build_standard_dataset
from src.strategies.context import MarketContext
from src.strategies.runner import run_strategies
from src.backtest.engine import run_backtest
from src.backtest.portfolio import combine_trades, strategy_correlation
from src.research.analysis_utils import group_metrics
from src.research.strategy_analysis import analyze_strategies
from src.research.portfolio_research import analyze_portfolio_combinations, portfolio_correlation_summary
from src.research.market_conditions import classify_market_conditions, label_trades_with_conditions
from src.research.session_analysis import label_trade_sessions, analyze_sessions
from src.research.confidence_analysis import confidence_profitability_correlation, analyze_confidence_buckets
from src.research.walkforward_research import generate_rolling_windows, evaluate_rolling_windows, summarize_stability
from src.research.parameter_sweep import coordinate_sweep
from src.research.institutional_edge import (
    compute_negative_regime_buckets, failure_frequency_report,
    IESInputs, compute_institutional_edge_scores, avg_abs_correlation, best_diversification_benefit,
)
from src.utils.perf import ProgressReporter

STRATEGY_IDS = ["S1", "S2", "S3", "S4", "S5"]
STARTING_BALANCE = 10_000.0

# CRITICAL FINDING (documented in the final report, not silently worked
# around): src.backtest.risk.RiskTracker.consecutive_losses only resets
# on a WINNING trade close -- but once max_consecutive_losses is reached,
# can_open() rejects every subsequent signal, so no trade can ever open
# to produce the winning close that would reset it. This is a permanent
# lockout, not a temporary pause: on a full multi-year backtest of any
# strategy with a sub-50% win rate (all five strategies here, on this
# dataset -- see the Strategy Analysis section), it triggers early and
# then rejects ~97% of all remaining signals for years. Discovered via
# this task's failure-rate sanity check (num_trades in the strategy
# comparison table was far lower than the signal count), not assumed.
#
# Per this task's explicit scope ("no further infrastructure work"), the
# fix here is a RESEARCH-appropriate risk config, not a code change to
# risk.py: max_consecutive_losses is effectively disabled (999) so each
# strategy's full signal quality can be evaluated across its entire
# history, which is what "does it remain profitable over multiple years"
# requires. A live-trading deployment would still want SOME consecutive-
# loss circuit breaker -- but one that resets after a cooldown period,
# not a permanent lockout -- and that fix is recommended as follow-up
# infrastructure work in this report's Recommendations section.
RESEARCH_RISK_CONFIG = replace(DEFAULT_RISK_CONFIG, max_consecutive_losses=999)

# symbol -> (path, provider, source_tz)
# NOTE: download_history.py's --build-m1 writes the campaign-wide
# (deduplicated, appended-across-days) M1 CSV to
# data/raw/dukascopy/{SYMBOL}_M1.csv (see campaign_m1_path in
# src/data/providers/dukascopy.py) -- not data/raw/{SYMBOL}_M1.parquet.
SYMBOL_SOURCES = {
    "EURUSD": ("data/raw/EURUSD_M1_histdata.parquet", "histdata", "UTC"),
    "GBPUSD": ("data/raw/dukascopy/GBPUSD_M1.csv", "dukascopy", "UTC"),
    "USDJPY": ("data/raw/dukascopy/USDJPY_M1.csv", "dukascopy", "UTC"),
    "AUDUSD": ("data/raw/dukascopy/AUDUSD_M1.csv", "dukascopy", "UTC"),
    "USDCAD": ("data/raw/dukascopy/USDCAD_M1.csv", "dukascopy", "UTC"),
    "USDCHF": ("data/raw/dukascopy/USDCHF_M1.csv", "dukascopy", "UTC"),
    "NZDUSD": ("data/raw/dukascopy/NZDUSD_M1.csv", "dukascopy", "UTC"),
    # XAUUSD deliberately excluded from this research campaign per explicit
    # user instruction ("we don't want to trade XAUUSD for now") -- data
    # acquisition was successfully unblocked (see the empty-response fix in
    # src/data/providers/dukascopy.py) but the symbol is out of scope by
    # request, not by data availability. Documented in the report as an
    # intentional scope exclusion, not a limitation.
}

OUT_DIR = ROOT / "reports" / "institutional_research"
CACHE_DIR = OUT_DIR / "_cache"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def discover_available_symbols() -> dict:
    """Returns {symbol: m1_dataframe} for every symbol whose raw file
    actually exists -- symbols with no data are reported as unavailable,
    per the task brief's explicit "document the limitation" instruction."""
    available, missing = {}, []
    for symbol, (rel_path, provider, tz) in SYMBOL_SOURCES.items():
        path = ROOT / rel_path
        if not path.exists():
            missing.append(symbol)
            continue
        dataset = build_standard_dataset(path, provider=provider, symbol=symbol, timeframe="M1", source_tz=tz, expected_interval="1min")
        m1 = dataset.data[["timestamp", "open", "high", "low", "close"]].copy()
        if "volume" in dataset.data.columns:
            m1["volume"] = dataset.data["volume"]
        available[symbol] = m1
        log(f"Loaded {symbol}: {len(m1)} candles, {m1['timestamp'].iloc[0]} -> {m1['timestamp'].iloc[-1]}, quality={dataset.report.quality_score}")
    if missing:
        log(f"UNAVAILABLE (no data acquired): {missing} -- see docs/INSTITUTIONAL_RESEARCH_REPORT.md data-scope note")
    return available, missing


# ---------------------------------------------------------------------------
# Tier 1: per-symbol independent strategy runs + combinations
# ---------------------------------------------------------------------------

def run_symbol_strategies_independently(symbol: str, m1: pd.DataFrame, progress: ProgressReporter | None = None) -> tuple:
    """ONE MarketContext, ONE run_strategies call (all 5 strategies'
    signals generated together -- this is the expensive step), then FIVE
    INDEPENDENT run_backtest calls (fresh RiskTracker each, per the task
    brief's "run each independently"), each on only that strategy's own
    signal subset."""
    context = MarketContext(symbol=symbol, m1=m1)
    all_signals = run_strategies(context)
    if progress is not None:
        progress.checkpoint(f"{symbol}:strategies", int(len(m1)))

    trades_by_strategy = {}
    for sid in STRATEGY_IDS:
        sid_signals = [s for s in all_signals if s.strategy_id == sid]
        trades = run_backtest(sid_signals, m1, context=context, risk_config=RESEARCH_RISK_CONFIG)
        trades_by_strategy[sid] = trades
        if progress is not None:
            progress.checkpoint(f"{symbol}:backtest:{sid}", int(len(m1) / len(STRATEGY_IDS)))
    return context, trades_by_strategy


def regime_breakdown(symbol: str, trades_by_strategy: dict) -> pd.DataFrame:
    dims = [
        ("trend_state", lambda t: t.metadata.get("trend_state", "unknown")),
        ("volatility_state", lambda t: t.metadata.get("volatility_state", "unknown")),
        ("directional_bias", lambda t: t.metadata.get("directional_bias", "unknown")),
        ("gap_day", lambda t: "gap_day" if t.metadata.get("is_gap_day") else "normal_day"),
        ("session", lambda t: t.session or "unknown"),
        ("news_day", lambda t: "news_day" if t.metadata.get("is_news_day") else "normal_day"),
    ]
    rows = []
    for sid, trades in trades_by_strategy.items():
        for dim_name, key_fn in dims:
            df = group_metrics(trades, key_fn, STARTING_BALANCE)
            if df.empty:
                continue
            df.insert(0, "dimension", dim_name)
            df.insert(0, "strategy_id", sid)
            df.insert(0, "symbol", symbol)
            rows.append(df.rename(columns={"group": "condition"}))
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def per_symbol_tier1(symbol: str, m1: pd.DataFrame, progress: ProgressReporter | None = None) -> dict:
    log(f"=== Tier 1: {symbol} ({len(m1)} candles) ===")
    context, trades_by_strategy = run_symbol_strategies_independently(symbol, m1, progress)

    conditions = classify_market_conditions(m1)
    for trades in trades_by_strategy.values():
        label_trades_with_conditions(trades, conditions)
        label_trade_sessions(trades, context)

    all_trades = combine_trades(trades_by_strategy)
    strategy_metrics = analyze_strategies(trades_by_strategy, STARTING_BALANCE)
    strategy_metrics.insert(0, "symbol", symbol)

    portfolio_analysis = analyze_portfolio_combinations(trades_by_strategy, STARTING_BALANCE)
    portfolio_analysis.insert(0, "symbol", symbol)
    correlation = portfolio_correlation_summary(trades_by_strategy)

    confidence_corr = confidence_profitability_correlation(all_trades)
    confidence_corr["symbol"] = symbol
    confidence_buckets = analyze_confidence_buckets(all_trades, STARTING_BALANCE)
    confidence_buckets.insert(0, "symbol", symbol)

    session_perf = analyze_sessions(all_trades, STARTING_BALANCE)
    session_perf.insert(0, "symbol", symbol)

    regime = regime_breakdown(symbol, trades_by_strategy)

    neg_regimes = compute_negative_regime_buckets(trades_by_strategy)
    failure = failure_frequency_report(trades_by_strategy, neg_regimes)
    failure.insert(0, "symbol", symbol)

    num_signals_total = sum(len(t) for t in trades_by_strategy.values())
    log(f"=== {symbol} Tier 1 complete: {num_signals_total} trades across 5 strategies ===")

    return {
        "symbol": symbol,
        "context": context,
        "m1": m1,
        "trades_by_strategy": trades_by_strategy,
        "strategy_metrics": strategy_metrics,
        "portfolio_analysis": portfolio_analysis,
        "correlation": correlation,
        "confidence_corr": confidence_corr,
        "confidence_buckets": confidence_buckets,
        "session_perf": session_perf,
        "regime": regime,
        "failure": failure,
    }


# ---------------------------------------------------------------------------
# Tier 2: EURUSD-only robustness (year/month/rolling windows)
# ---------------------------------------------------------------------------

def robustness_tier2(symbol: str, m1: pd.DataFrame, trades_by_strategy: dict) -> dict:
    log(f"=== Tier 2: robustness ({symbol}) ===")
    year_rows, month_rows = [], []
    for sid, trades in trades_by_strategy.items():
        df_year = group_metrics(trades, lambda t: t.entry_timestamp.year if t.entry_timestamp is not None else "n/a", STARTING_BALANCE)
        df_year.insert(0, "strategy_id", sid)
        year_rows.append(df_year.rename(columns={"group": "year"}))

        df_month = group_metrics(trades, lambda t: t.entry_timestamp.strftime("%Y-%m") if t.entry_timestamp is not None else "n/a", STARTING_BALANCE)
        df_month.insert(0, "strategy_id", sid)
        month_rows.append(df_month.rename(columns={"group": "year_month"}))

    year_by_year = pd.concat(year_rows, ignore_index=True) if year_rows else pd.DataFrame()
    month_by_month = pd.concat(month_rows, ignore_index=True) if month_rows else pd.DataFrame()

    windows_6mo = generate_rolling_windows(m1, test_days=182, step_days=182, train_days=182)
    windows_12mo = generate_rolling_windows(m1, test_days=365, step_days=365, train_days=365)
    log(f"  {len(windows_6mo)} rolling 6mo windows, {len(windows_12mo)} rolling 12mo windows")

    stability_rows = []
    window_detail = {}
    for sid, trades in trades_by_strategy.items():
        eval6 = evaluate_rolling_windows(trades, windows_6mo, STARTING_BALANCE)
        eval12 = evaluate_rolling_windows(trades, windows_12mo, STARTING_BALANCE)
        window_detail[(sid, "6mo")] = eval6
        window_detail[(sid, "12mo")] = eval12
        stab6, stab12 = summarize_stability(eval6), summarize_stability(eval12)
        stability_rows.append({"strategy_id": sid, "window": "6mo", **stab6})
        stability_rows.append({"strategy_id": sid, "window": "12mo", **stab12})

    stability_summary = pd.DataFrame(stability_rows)
    log(f"=== Tier 2 complete ===")
    return {
        "year_by_year": year_by_year, "month_by_month": month_by_month,
        "stability_summary": stability_summary, "window_detail": window_detail,
    }


# ---------------------------------------------------------------------------
# Tier 3: EURUSD 1-year slice parameter robustness
# ---------------------------------------------------------------------------

def parameter_robustness_tier3(symbol: str, m1_full: pd.DataFrame) -> dict:
    # Task 8 scope note: `run_experiment` (src/research/experiment.py)
    # regenerates ALL 5 strategies' signals on every sweep candidate,
    # regardless of `strategy_filter` -- an existing Task 5 architectural
    # choice, not something this task redesigns. A smoke test showed
    # ~1.8 min/experiment even on a 78-day slice; a full 1-year slice
    # extrapolates to multiple hours for the ~19 experiments below. A
    # 3-month slice keeps this tractable while still being real market
    # data -- sufficient to reveal each parameter's qualitative
    # sensitivity (is the strategy robust or overfit to this setting),
    # which is what this tier asks for; documented as a reduced-scope
    # test in the report, not silently narrowed.
    log(f"=== Tier 3: parameter robustness ({symbol}, 3mo slice) ===")
    start = m1_full["timestamp"].iloc[0]
    end = start + pd.Timedelta(days=91)
    m1 = m1_full[m1_full["timestamp"] < end].reset_index(drop=True)
    log(f"  slice: {len(m1)} candles, {m1['timestamp'].iloc[0]} -> {m1['timestamp'].iloc[-1]}")
    context = MarketContext(symbol=symbol, m1=m1)

    base_configs = {
        "S1": DEFAULT_S1_CONFIG, "S2": DEFAULT_S2_CONFIG, "S3": DEFAULT_S3_CONFIG,
        "S4": DEFAULT_S4_CONFIG, "S5": DEFAULT_S5_CONFIG, "tp_config": DEFAULT_TAKE_PROFIT_CONFIG,
        "risk_config": RESEARCH_RISK_CONFIG,
    }

    results = {}

    # Risk:Reward -- applies across all strategies (tp_config is shared).
    rr_sweep = coordinate_sweep(
        symbol, m1, context, base_configs,
        [("tp_config", "risk_reward", [1.0, 1.5, 2.0, 2.5, 3.0])],
    )
    results["risk_reward"] = rr_sweep["history"]

    # S1 gap size (S1's defining parameter).
    gap_sweep = coordinate_sweep(
        symbol, m1, context, base_configs,
        [("S1", "min_gap_size", [0.0003, 0.0005, 0.0008, 0.0012, 0.0018])],
        strategy_filter=["S1"],
    )
    results["s1_gap_size"] = gap_sweep["history"]

    # CHoCH confirmation timeframe -- tested on S3 (liquidity sweep reversal, most CHoCH-dependent).
    choch_sweep = coordinate_sweep(
        symbol, m1, context, base_configs,
        [("S3", "choch_timeframe", ["M1", "M5", "M15"])],
        strategy_filter=["S3"],
    )
    results["s3_choch_timeframe"] = choch_sweep["history"]

    # Order Block freshness requirement -- tested on S3.
    ob_sweep = coordinate_sweep(
        symbol, m1, context, base_configs,
        [("S3", "require_fresh_ob", [True, False])],
        strategy_filter=["S3"],
    )
    results["s3_require_fresh_ob"] = ob_sweep["history"]

    # Session filter -- tested on S5 (Asian range sweep, inherently session-driven).
    session_sweep = coordinate_sweep(
        symbol, m1, context, base_configs,
        [("S5", "session_filter", [None, ("london",), ("new_york",), ("london", "new_york")])],
        strategy_filter=["S5"],
    )
    results["s5_session_filter"] = session_sweep["history"]

    log("=== Tier 3 complete ===")
    return results


# ---------------------------------------------------------------------------
# Cross-symbol aggregation + IES
# ---------------------------------------------------------------------------

def aggregate_trades_by_strategy_all_symbols(per_symbol_results: dict) -> dict:
    combined: dict = {sid: [] for sid in STRATEGY_IDS}
    for res in per_symbol_results.values():
        for sid, trades in res["trades_by_strategy"].items():
            combined[sid].extend(trades)
    return combined


def compute_ies_table(combined_trades_by_strategy: dict, portfolio_analysis_combined: pd.DataFrame,
                       correlation_combined: pd.DataFrame, stability_summary_eurusd: pd.DataFrame) -> pd.DataFrame:
    inputs = []
    for sid in STRATEGY_IDS:
        trades = combined_trades_by_strategy[sid]
        metrics = analyze_strategies({sid: trades}, STARTING_BALANCE)
        if metrics.empty:
            continue
        row = metrics.iloc[0]

        stab = stability_summary_eurusd[(stability_summary_eurusd.strategy_id == sid) & (stability_summary_eurusd.window == "12mo")]
        positive_window_pct = float(stab["positive_window_pct"].iloc[0]) if not stab.empty and "positive_window_pct" in stab.columns else 0.0
        exp_mean = float(stab["expectancy_mean"].iloc[0]) if not stab.empty and "expectancy_mean" in stab.columns else 0.0
        exp_std = float(stab["expectancy_std"].iloc[0]) if not stab.empty and "expectancy_std" in stab.columns else 0.0

        inputs.append(IESInputs(
            strategy_id=sid,
            r_multiple_mean=float(row["r_multiple_mean"]),
            profit_factor=float(row["profit_factor"]) if row["profit_factor"] != float("inf") else 10.0,
            max_drawdown_pct=float(row["max_drawdown_pct"]),
            positive_window_pct=positive_window_pct,
            expectancy_std_across_windows=exp_std,
            expectancy_mean_across_windows=exp_mean,
            portfolio_diversification_benefit=best_diversification_benefit(portfolio_analysis_combined, sid),
            avg_abs_correlation_with_others=avg_abs_correlation(correlation_combined, sid),
        ))
    return compute_institutional_edge_scores(inputs)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _load_cache(path: Path):
    if path.exists():
        try:
            with open(path, "rb") as f:
                return pickle.load(f)
        except Exception as exc:
            log(f"  cache at {path.name} unreadable ({exc}), recomputing")
    return None


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume", action="store_true", help="Reuse cached per-symbol/tier results from a previous run instead of recomputing them.")
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    available, missing = discover_available_symbols()
    if not available:
        log("No symbols available -- aborting.")
        return 1

    total_candles = sum(len(m1) for m1 in available.values())
    progress = ProgressReporter(total_candles, label="institutional_research").start()

    per_symbol_results = {}
    for symbol, m1 in available.items():
        cached = _load_cache(CACHE_DIR / f"{symbol}_tier1.pkl") if args.resume else None
        if cached is not None:
            log(f"{symbol} Tier 1: reused from cache ({len(cached['m1'])} candles)")
            per_symbol_results[symbol] = cached
            continue
        t0 = time.time()
        res = per_symbol_tier1(symbol, m1, progress)
        per_symbol_results[symbol] = res
        with open(CACHE_DIR / f"{symbol}_tier1.pkl", "wb") as f:
            pickle.dump(res, f)
        log(f"{symbol} Tier 1 took {time.time() - t0:.1f}s")

    # Tier 2 + 3: EURUSD only (the one symbol with multi-year depth).
    tier2 = tier3 = None
    if "EURUSD" in per_symbol_results:
        eur = per_symbol_results["EURUSD"]

        tier2 = _load_cache(CACHE_DIR / "EURUSD_tier2.pkl") if args.resume else None
        if tier2 is None:
            t0 = time.time()
            tier2 = robustness_tier2("EURUSD", eur["m1"], eur["trades_by_strategy"])
            with open(CACHE_DIR / "EURUSD_tier2.pkl", "wb") as f:
                pickle.dump(tier2, f)
            log(f"EURUSD Tier 2 took {time.time() - t0:.1f}s")
        else:
            log("EURUSD Tier 2: reused from cache")

        tier3 = _load_cache(CACHE_DIR / "EURUSD_tier3.pkl") if args.resume else None
        if tier3 is None:
            t0 = time.time()
            tier3 = parameter_robustness_tier3("EURUSD", eur["m1"])
            with open(CACHE_DIR / "EURUSD_tier3.pkl", "wb") as f:
                pickle.dump(tier3, f)
            log(f"EURUSD Tier 3 took {time.time() - t0:.1f}s")
        else:
            log("EURUSD Tier 3: reused from cache")
    else:
        log("EURUSD not available -- Tier 2/3 (robustness + parameter sweeps) skipped entirely; document this limitation.")

    # Cross-symbol aggregation for IES.
    combined_trades = aggregate_trades_by_strategy_all_symbols(per_symbol_results)
    portfolio_combined = analyze_portfolio_combinations(combined_trades, STARTING_BALANCE)
    correlation_combined = portfolio_correlation_summary(combined_trades)
    stability_summary = tier2["stability_summary"] if tier2 else pd.DataFrame(columns=["strategy_id", "window", "positive_window_pct", "expectancy_mean", "expectancy_std"])
    ies_table = compute_ies_table(combined_trades, portfolio_combined, correlation_combined, stability_summary)

    with open(CACHE_DIR / "aggregate.pkl", "wb") as f:
        pickle.dump({
            "combined_trades": combined_trades, "portfolio_combined": portfolio_combined,
            "correlation_combined": correlation_combined, "ies_table": ies_table,
            "missing_symbols": missing, "symbols_used": list(available.keys()),
        }, f)

    progress.stop()
    log("=== ALL TIERS COMPLETE ===")
    log(f"IES ranking:\n{ies_table[['strategy_id', 'institutional_edge_score']].to_string(index=False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
