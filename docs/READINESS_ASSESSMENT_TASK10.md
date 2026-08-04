# Readiness Assessment — "If this platform had to trade tomorrow"

Every recommendation below is supported by a specific Task 8, 9, or 10 finding — cited inline, not asserted.

## Which strategies would be enabled?

**S3 (Liquidity Sweep Reversal) and S4 (Previous Day High/Low Sweep)** — the only two strategies ranked in the top tier by Institutional Edge Score in Task 8 (S3: 71.74, S4: 64.30 of 5) and the only two with positive expectancy on every one of the 7 symbols tested (Task 8 §4.1). Task 9's refinement work (Order Block freshness/quality gating, ITQS) was built specifically for these two.

**S2 (Third BOS Continuation) and S5 (Asian Range Sweep)** should remain **disabled at initial launch**, held as secondary candidates — Task 8 found both solidly profitable (IES 64.06, 64.78) but S2's losses are dominated by an unexplained "Other" failure category (Task 9 Phase 8) and neither received Task 9's refinement/ITQS treatment. Enable only after repeating Task 9's Phase 1-4 analysis for them specifically.

**S1 (Monday Gap Reversion) must not be enabled** — negative expectancy on its only tested symbol, worst IES score (33.37), zero signals generated on 6 of 7 symbols in their available windows, and a flat-negative parameter response curve on its own defining parameter (Task 8 §11, Task 9 confirms no rescue via refinement).

## Which symbols would be traded?

**EURUSD with full confidence** — the only symbol with multi-year validated history (Task 8 §3), and the only symbol Task 10's paper trading simulation could evaluate at meaningful sample depth.

**GBPUSD, USDJPY, AUDUSD, USDCAD, USDCHF, NZDUSD at reduced size / directional confidence only** — 6 months of real data each (Task 8 §3 scope note), sufficient to confirm the SAME edge direction holds (Task 8 §4.1: S3/S4 positive on every one of these) but not sufficient for the same statistical confidence as EURUSD. Task 9 Phase 8's star ratings should govern relative sizing within this group: **S3 → prioritize USDJPY (★★★★★), avoid/minimize USDCAD (★☆☆☆☆)**; **S4 → prioritize NZDUSD/USDJPY/GBPUSD (★★★★☆), avoid/minimize AUDUSD/USDCHF (★☆☆☆☆)**.

**XAUUSD should remain excluded** — out of scope per explicit instruction in Task 8/9, and no data was acquired for it in this platform to date.

## Maximum portfolio risk?

**3.0%** total open-position risk (`AllocationLimits.max_portfolio_risk_pct`, `RiskLimits.max_portfolio_heat_pct` — Task 10 Phase 3/5), consistent with the platform's existing `RiskConfig.max_portfolio_exposure_pct` convention (Task 4) rather than a newly invented number.

## Daily trade limit?

**5 simultaneous positions maximum** (`AllocationLimits.max_simultaneous_trades`), with at most **3 concurrent per strategy** and **2 per currency** — Task 10's paper trading run shows this constraint bound meaningfully often (233 rejections for EUR exposure, 142 for USD, out of 3,187 opportunities), i.e. it is not a vacuous limit; it is actively shaping which trades get taken. No explicit "trades per calendar day" cap beyond these concurrency limits is recommended at this stage — the concurrency limits already bound daily activity in practice.

## Preferred sessions?

**S3 → London** (Task 9 Phase 7: weighted expectancy 43.3 on n=343, the strongest reliable session finding for S3). **S4 → Tokyo primary, London secondary** (Task 9 Phase 7: Tokyo weighted expectancy 161.2 on n=344 — the single largest reliable regime effect measured in the entire research program). Both strategies should avoid the Sydney/Asian session per Task 9 Phase 10's explicit filter rejection.

## Recommended paper-trading duration before live deployment?

**A minimum of 3 months, targeting at least 6 months if IOS tier A/B trade volume is low.** Evidence: Task 10's own paper-trading validation found only 56 IOS tier A/B trades (the highest-conviction tier, with the strongest win-rate edge: 61.5%/65.1% vs. 46-48% for tiers C/D — `OPPORTUNITY_RANKING_REPORT.md`) across a combined ~7 symbol-years of data. This is the platform's own evidence that high-conviction opportunities are rare; a paper-trading window shorter than 3 months is unlikely to accumulate enough tier-A/B trades to confirm the win-rate edge holds prospectively rather than only in the retrospective sample it was measured on. Track the SAME metrics used in this validation (win rate by IOS tier, selected-vs-baseline expectancy/profit-factor) throughout the paper-trading period as the explicit go/no-live criterion — if tier A/B win rate materially underperforms the 61-65% figure found here after a statistically meaningful sample, do not proceed to live capital.

## Explicit non-recommendations

- Do not enable S1 in any capacity.
- Do not trade XAUUSD until data is acquired and a dedicated Task 8/9-style research pass is run on it.
- Do not treat GBPUSD/USDJPY/AUDUSD/USDCAD/USDCHF/NZDUSD results as equally validated to EURUSD — size accordingly (smaller allocations, tighter monitoring) until multi-year data is acquired for them (Task 8 §12 Future Research already recommends this).
- Do not remove or relax the account-level risk gate (daily/weekly/monthly loss limits) to increase trade throughput — Task 10's own data shows these limits are frequently binding by design on a small, non-compounding account; before loosening them, first confirm the effect is a starting-balance artifact (see `OPPORTUNITY_RANKING_REPORT.md`'s caveat) rather than loosening the limits themselves.
