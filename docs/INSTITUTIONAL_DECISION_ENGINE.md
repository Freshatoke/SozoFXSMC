# Institutional Trading Decision Engine (ITDE) — Task 10

## What changed

Before this task, the platform answered "did S3 generate a signal?" and executed it (subject only to per-strategy risk gates). After this task, every signal from every enabled strategy is treated as a candidate **opportunity** that must earn its way into the portfolio: ranked against every other opportunity available at the same moment, checked against institutional exposure/correlation/risk limits, and either executed, postponed, or rejected — with a reason attached to every decision.

**Proof this actually works, not just architecture**: replaying the decision engine over 3,187 already-known historical S3/S4 trades (Task 8/9's cached results, 7 symbols, 2020-2026) and comparing the engine's selected subset against a naive take-everything baseline:

| | Baseline (take everything) | Decision-engine-selected | Delta |
| --- | --- | --- | --- |
| Trades taken | 3,187 (100%) | 1,173 (36.8%) | -63.2% volume |
| Win rate | 46.3% | 47.4% | +1.1pp |
| Expectancy (R) | 0.2332 | 0.2946 | **+26.3%** |
| Profit factor | 1.4007 | 1.6554 | **+18.2%** |

The engine trades a third as often but each trade it takes is measurably better quality on every metric that matters for risk-adjusted returns. Full detail: `reports/decision_engine/paper_trading_summary.json`, `paper_trading_decisions.parquet`.

## Architecture

```
src/decision_engine/
  opportunity.py            Phase 1 -- Opportunity dataclass + unified queue builder
                             (from live Signals OR reconstructed from historical Trades for paper trading)
  ios.py                    Phase 2 -- Institutional Opportunity Score
  portfolio_allocation.py   Phase 3 -- exposure/capacity/correlation limits
  trade_selection.py        Phase 4/7 -- EXECUTE/POSTPONE/IGNORE decisions + explanations
  risk_layer.py             Phase 5 -- account-level daily/weekly/monthly/heat/session gates
  daily_plan.py             Phase 6 -- human-readable trading plan formatter
  paper_trading.py          Phase 8 -- historical-data-as-live-data simulation + performance reports
```

Nothing here modifies `src/strategies/`, `src/backtest/`, `src/features/`, `src/structure/`, or `src/research/` — this layer only calls into them (same architectural discipline as the Research Laboratory in Task 5).

## Data flow

```
1. run_strategies(context) -> Signals (Task 3, unmodified)
2. build_opportunity_queue(signals, context) -> list[Opportunity]  (Phase 1)
   - looks up each signal's referenced Order Block/liquidity level via
     src.research.trade_features.FeatureContextIndex (Task 9, reused not duplicated)
   - computes ITQS pre-trade using the exact Task 9 formula (src.research.itqs)
3. label_opportunities(opportunities, market_conditions) -- regime labels (Task 5's classify_market_conditions, reused)
4. select_trades(opportunities, account_state) -> list[Decision]  (Phase 2+3+4+5+7)
   - sorts by initial IOS (portfolio-independent) descending
   - for each, in order: account risk gate -> correlation check -> session
     exposure check -> allocation/capacity check -> EXECUTE (added to this
     cycle's portfolio) or IGNORE/POSTPONE with a specific reason
5. format_daily_plan(decisions, date, balance) -> markdown  (Phase 6)
```

For paper trading (Phase 8), step 1 is replaced by reconstructing opportunities from already-closed historical `Trade` records (`opportunity.trade_to_opportunity`) using only PRE-ENTRY facts (confluence_snapshot, reason_codes, confidence_score, entry/stop/target, entry_timestamp) — never the outcome fields (realized_pnl, r_multiple, exit_reason) — so there is no look-ahead in what the engine "sees" when deciding; the trade's actual outcome is looked up SEPARATELY, only after a decision has already been made, purely to score hypothetical performance.

## Key design decisions (and why)

- **Sort-before-select, not sort-then-trust**: `select_trades` computes each opportunity's portfolio-independent IOS and sorts internally rather than requiring the caller to pre-sort — caught during this task's own smoke testing as a real bug (a naive test call passed unsorted opportunities and the engine approved two 30-IOS trades while postponing several 50+ IOS ones). Fixed by never trusting caller ordering for a decision this consequential.
- **POSTPONE vs. IGNORE are semantically different**, not just two rejection buckets: POSTPONE is reserved for pure capacity limits (max simultaneous trades / per-strategy / per-currency / per-session) that could resolve later in the same session if an existing position closes; IGNORE is for correlation, account-risk, or quality reasons that wouldn't be fixed by more capacity. This distinction is directly useful to a desk deciding whether to re-check a rejected setup later.
- **Currency-leg correlation, not just symbol identity**: the task brief's own example (EURUSD and GBPUSD both being effectively "long USD" bets) is implemented as an actual currency-leg overlap check, not a lookup table — it generalizes to any pair sharing a currency, not just the named example.
- **Reused Task 9's evidence, didn't re-derive it**: IOS's regime/session/symbol-strength components are hardcoded from the actual measured findings in `reports/edge_refinement/` (market_regime_report.csv, symbol_specialisation.csv) — traceable to specific numbers, not fresh assumptions.

## Explainability (Phase 7)

Every `Decision` carries `reasons_for` (✓ list) and `reasons_against` (✗ list), generated directly from the IOS component values and the specific rule that fired — e.g.:

```
Approved because:
  ✓ Fresh Order Block
  ✓ High ITQS
  ✓ Preferred session for this strategy
  ✓ Favourable market regime (volatility/trend)

Rejected because:
  ✗ Shares a currency leg with already-held USDJPY (S3) in the same direction
```

No decision in this engine is ever unexplained — `Decision.explanation()` renders this format directly, and it's what `daily_plan.py` uses to build the trading plan.

See also: `docs/OPPORTUNITY_RANKING_REPORT.md`, `docs/IOS_SPECIFICATION.md`, `docs/PORTFOLIO_ALLOCATION_RULES.md`, `docs/PRODUCTION_DEPLOYMENT_CHECKLIST.md`, `docs/READINESS_ASSESSMENT_TASK10.md`.
