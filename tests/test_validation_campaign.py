import pandas as pd

from src.research.validation_campaign import (
    DatasetSpec,
    _market_condition_frames,
    discover_dataset_specs,
    run_validation_campaign,
)
from src.backtest.trade import Trade, TradeStatus
from tests.helpers import make_candles, make_multi_day_m1


def _closed_trade(strategy_id, symbol):
    ts = pd.Timestamp("2024-01-01", tz="UTC")
    return Trade(
        trade_id=f"T_{strategy_id}_{symbol}", signal_id="S", strategy_id=strategy_id, symbol=symbol,
        timeframe="M1", direction="bullish", signal_timestamp=ts, confidence_score=90.0,
        reason_codes=[strategy_id], confluence_snapshot={}, entry_method="market",
        stop_method="fixed_pips", take_profit_method="fixed_rr", status=TradeStatus.CLOSED.value,
        entry_price=1.10, exit_price=1.101, entry_timestamp=ts, exit_timestamp=ts,
        exit_reason="TAKE_PROFIT", realized_pnl=100.0, r_multiple=1.0, duration_candles=5,
        metadata={"trend_state": "trending"}, session="london",
    )


def test_market_condition_frames_include_symbol_column_for_multi_symbol_campaigns():
    """Regression test: rows from different symbols used to be
    indistinguishable in the concatenated market_condition_analysis
    output (no symbol column), so e.g. two "trending" rows -- one from
    EURUSD, one from GBPUSD -- could not be told apart after concat."""
    eurusd_frames = _market_condition_frames([_closed_trade("S3", "EURUSD")], 10_000, "EURUSD")
    gbpusd_frames = _market_condition_frames([_closed_trade("S3", "GBPUSD")], 10_000, "GBPUSD")
    combined = pd.concat(eurusd_frames + gbpusd_frames, ignore_index=True)

    assert "symbol" in combined.columns
    assert set(combined["symbol"]) == {"EURUSD", "GBPUSD"}


def test_discover_dataset_specs_ignores_synthetic_files(tmp_path):
    (tmp_path / "EURUSD_M1_synthetic.csv").write_text("timestamp,open,high,low,close\n")
    real = tmp_path / "GBPUSD_M1.csv"
    real.write_text("timestamp,open,high,low,close\n")

    specs = discover_dataset_specs(tmp_path, symbols=("EURUSD", "GBPUSD"))

    assert [s.symbol for s in specs] == ["GBPUSD"]
    assert specs[0].path == real


def test_validation_campaign_writes_empty_exports(tmp_path):
    result = run_validation_campaign(
        [],
        out_dir=tmp_path / "reports",
        processed_dir=tmp_path / "processed",
    )

    assert result.datasets == []
    assert (tmp_path / "reports" / "validation_summary.md").exists()
    assert (tmp_path / "reports" / "strategy_rankings.csv").exists()
    assert (tmp_path / "reports" / "research_dashboard.html").exists()


def test_validation_campaign_runs_small_real_dataset(tmp_path):
    rows = [
        (1.1000, 1.1010, 1.0990, 1.1005),
        (1.1005, 1.1020, 1.1000, 1.1015),
        (1.1015, 1.1030, 1.1010, 1.1025),
        (1.1025, 1.1040, 1.1020, 1.1035),
        (1.1035, 1.1050, 1.1030, 1.1045),
    ]
    df = make_candles(rows)
    path = tmp_path / "EURUSD_M1.csv"
    out = df.copy()
    out["timestamp"] = out["timestamp"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    out.to_csv(path, index=False)

    result = run_validation_campaign(
        [DatasetSpec(symbol="EURUSD", path=path, provider="csv")],
        out_dir=tmp_path / "reports",
        processed_dir=tmp_path / "processed",
    )

    assert result.datasets[0]["symbol"] == "EURUSD"
    assert result.datasets[0]["candles"] == 5
    assert (tmp_path / "reports" / "confidence_validation.parquet").exists()
    assert pd.read_parquet(tmp_path / "processed" / "EURUSD_M1.parquet").shape[0] == 5


def test_validation_campaign_survives_dataset_with_a_weekend_gap_day(tmp_path):
    """Regression test for a crash found by actually RUNNING the campaign
    end to end (not caught by the tiny 5-row fixture above, which has no
    weekend in it): the "gap_day" market-condition dimension used to
    yield Python bools while every other dimension (trend/volatility/
    bias/session) yields strings. Once concatenated into one "condition"
    column, PyArrow refused to write the mixed-type column to Parquet --
    `ArrowTypeError: Expected bytes, got a 'bool' object` -- meaning the
    campaign crashed on ANY dataset spanning a real weekend, which is
    every realistic multi-week FX dataset. Fixed by making gap_day yield
    "gap_day"/"normal_day" strings instead of True/False."""
    m1 = make_multi_day_m1(num_days=10)
    path = tmp_path / "EURUSD_M1.csv"
    out = m1.copy()
    out["timestamp"] = out["timestamp"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    out.to_csv(path, index=False)

    result = run_validation_campaign(
        [DatasetSpec(symbol="EURUSD", path=path, provider="csv")],
        out_dir=tmp_path / "reports",
        processed_dir=tmp_path / "processed",
    )

    market_conditions = pd.read_parquet(tmp_path / "reports" / "market_condition_analysis.parquet")
    gap_rows = market_conditions[market_conditions["condition_type"] == "gap_day"]
    assert set(gap_rows["condition"]).issubset({"gap_day", "normal_day"})
    assert "symbol" in market_conditions.columns

