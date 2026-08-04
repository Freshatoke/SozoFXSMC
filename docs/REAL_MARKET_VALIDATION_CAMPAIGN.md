# Real Market Validation Campaign

Task 7 validates the completed platform on real historical data. It does not
add strategy features, optimize parameters, or change the completed engines.

## Workflow

```
historical data
  -> Historical Data Pipeline
  -> Market Structure Engine
  -> SMC Feature Engine
  -> Strategy Engine
  -> Backtesting Engine
  -> Research Laboratory
  -> campaign exports
```

## Required data

Place real M1 CSV or Parquet files in `data/raw/`, one per symbol:

- EURUSD
- GBPUSD
- USDJPY
- AUDUSD
- USDCHF
- USDCAD
- NZDUSD
- XAUUSD

Dukascopy is the preferred provider. If another provider is used, pass
`--provider` and document the provider in the final report. Files containing
`synthetic` in the name are ignored by the campaign runner.

## Run

```bash
python scripts/run_validation_campaign.py \
  --raw-dir data/raw \
  --processed-dir data/processed/historical \
  --out-dir reports/validation_campaign \
  --provider dukascopy
```

## Exports

The campaign writes:

- `validation_report.pdf`
- `validation_summary.md`
- `strategy_rankings.csv`
- `portfolio_rankings.csv`
- `market_condition_analysis.parquet`
- `confidence_validation.parquet`
- `research_dashboard.html`
- `trade_history.parquet`
- `failure_analysis.parquet`
- `portfolio_correlations.parquet`
- `dataset_manifest.json`

## Interpretation

The campaign ranks strategies using expectancy, profit factor, recovery factor,
drawdown, and robustness-oriented grouping results. Win rate alone is not used
as evidence of edge.

Low trade counts should be treated as inconclusive. Negative expectancy across
symbols and regimes is grounds for rejection or quarantine before any future
optimization work.

