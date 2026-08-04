# Portfolio Allocation Rules — Task 10 Phase 3

Implemented in `src/decision_engine/portfolio_allocation.py`. Applied to every opportunity, in IOS-descending order, during a decision cycle.

## Limits (defaults — `AllocationLimits`)

| Limit | Default | Rationale |
| --- | --- | --- |
| Max simultaneous trades | 5 | Portfolio-wide, across every strategy and symbol combined |
| Max trades per currency | 2 | Prevents stacking correlated exposure to one currency across multiple symbols (e.g. EURUSD + GBPUSD both long-USD) |
| Max trades per strategy | 3 | Widened slightly from `RiskConfig.max_simultaneous_positions` (3, a single-strategy backtest default) — same number, now interpreted per-strategy across a multi-strategy desk |
| Max portfolio risk | 3.0% | Sum of allocated risk % across all open positions |
| Correlation reject threshold | 0.15 | Set directly above Task 8's own measured strategy-pair correlations (all five strategies pairwise <0.13, `reports/institutional_research/_cache/aggregate.pkl`) — anything above what this platform's own evidence shows as "normal" independence is treated as meaningfully correlated |

## Capital allocation

Risk per trade is set by IOS tier (`suggested_risk_pct`, see `IOS_SPECIFICATION.md`): 1.00% / 0.75% / 0.50% / 0.25% for tiers A/B/C/D respectively. Higher-conviction opportunities (by IOS, itself evidence-weighted) receive proportionally more capital — this is the institutional-desk principle "size up on your best ideas," implemented as a direct, transparent lookup rather than a discretionary judgment call.

## Correlation-adjusted selection (the task brief's own worked example)

Two checks, both evidence-based, applied via `is_correlated_with_portfolio`:

1. **Shared currency leg, same direction**: if EURUSD-long and GBPUSD-long are both candidates and one is already held, the other is rejected — "shares a currency leg... in the same direction" — regardless of which symbol appeared first; since opportunities are processed in IOS-descending order, the STRONGER setup is always the one already held when the weaker, correlated one is evaluated.
2. **Cross-strategy correlation above the Task 8 baseline** (0.15): uses the actual `strategy_correlation` matrix from Task 8, not an assumption.

## Capacity vs. rejection (POSTPONE vs. IGNORE)

Allocation-limit failures are classified by whether the constraint is a CAPACITY limit (max simultaneous / per-strategy / per-currency — could free up later) → **POSTPONE**, or a CORRELATION/QUALITY reason (not resolved by more capacity) → **IGNORE**. See `INSTITUTIONAL_DECISION_ENGINE.md` for the full rationale.

## Session exposure

A separate check (`risk_layer.check_session_exposure`) caps concurrent positions opened during any single session at 3 by default — an account-level risk concern (session-specific liquidity/volatility conditions affecting multiple open positions simultaneously), kept in the Risk Layer (Phase 5) rather than here since it's about account state, not per-opportunity allocation capacity.
