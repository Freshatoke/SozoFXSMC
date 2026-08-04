# Institutional Opportunity Score (IOS) — Specification

Implemented in `src/decision_engine/ios.py`. Ranks candidate opportunities BEFORE any trade decision is made — distinct from ITQS (Task 9), which scores an individual trade's quality; IOS additionally accounts for the CURRENT portfolio context (diversification) and market suitability (regime/session/symbol), which ITQS deliberately does not (ITQS is portfolio-agnostic by design).

## Formula

```
IOS = 100 × Σ (weight_i × component_i)
```

| Component | Weight | Source / evidence |
| --- | --- | --- |
| ITQS | 0.25 | Task 9 Phase 4 — the best available single per-trade quality signal (Spearman 0.06-0.10 vs. outcome) |
| Expected expectancy | 0.20 | This opportunity's own R:R × the strategy+symbol's historical win rate (Task 8/9 `symbol_specialisation.csv`), capped at a fixed reference ceiling |
| Order Block quality | 0.15 | Task 9 Phase 2 — significant for both S3 and S4 (t-test p<0.01) |
| Order Block freshness | 0.15 | Task 9 Phase 2/3 — the single strongest, most significant finding across the whole platform (chi2 p<0.001, FRESH win rate 71-73% vs MITIGATED 45-46%) |
| Market regime suitability | 0.10 | Task 9 Phase 7 — hardcoded preferred/avoid regimes per strategy |
| Session suitability | 0.05 | Task 9 Phase 7/10 |
| Symbol historical strength | 0.05 | Task 9 Phase 8 star ratings (`symbol_specialisation.csv` composite score) |
| Portfolio diversification | 0.05 | Computed live against the CURRENT decision cycle's already-approved opportunities |

Weights sum to 1.0. OB quality and OB freshness intentionally repeat their Task 9 emphasis even though they also feed into the ITQS component — this double-weighting is deliberate, not an oversight: they are the two strongest evidence-backed findings in the entire research program (Tasks 8 and 9 combined), and IOS is meant to rank live opportunities with that priority explicit and visible, not buried inside a single composite number.

## Component definitions

- **ITQS**: `opportunity.itqs / 100.0` — reuses `src.research.itqs.compute_itqs_row` exactly, computed pre-trade from the same fields (OB freshness/quality, displacement, liquidity strength, confidence score).
- **Expected expectancy**: `min(1.0, (expected_r × historical_win_rate) / 1.5)` — the divisor (1.5) is a FIXED reference ceiling (a 3.0 R:R at 50% historical win rate maps to 1.0), not fit to this dataset.
- **Order Block quality**: the referenced OB's own `quality_score` (platform-native, 0-1), clamped; 0.5 (neutral) if no OB is referenced.
- **Order Block freshness**: 1.0 if `FRESH`, 0.0 if `MITIGATED`, 0.5 if unknown/not referenced.
- **Market regime suitability**: starts at 0.5 neutral; raised to ≥0.85 if the opportunity's session is in that strategy's Task 9 Phase 7 preferred-session set, lowered to ≤0.15 if in the avoid-session set; further adjusted ±0.1-0.15 for volatility/trend/gap-day matches, all from Phase 7's actual measured findings.
- **Session suitability**: 1.0 preferred / 0.0 avoid / 0.5 neutral, per strategy, from the same Phase 7 findings.
- **Symbol historical strength**: the strategy+symbol's min-max normalized composite score from Task 9 Phase 8 (0=weakest of the 7 tested symbols, 1=strongest).
- **Portfolio diversification**: starts at 1.0, degrades by 0.2 for each of: shared strategy with an already-approved opportunity this cycle, shared currency leg, identical symbol — floored at 0.0.

## IOS tiers (used by Portfolio Allocation for risk sizing)

| Tier | IOS range | Suggested risk per trade |
| --- | --- | --- |
| A | ≥75 | 1.00% |
| B | 60-74.9 | 0.75% |
| C | 45-59.9 | 0.50% |
| D | <45 | 0.25% |

## Validation

Paper trading (Task 10 Phase 8, `reports/decision_engine/`) confirms IOS-based selection outperforms an unranked baseline: selected-subset win rate +1.1pp, expectancy +26.3%, profit factor +18.2% (n=1,173 selected of 3,187 available, 7 symbols, 2020-2026). This is the load-bearing validation for the whole IOS design — not a correlation coefficient in isolation, but an actual selected-vs-baseline trading outcome comparison.

## What IOS deliberately does NOT do

- It does not use any trained model, embedding, or black-box AI component — every input is either a platform-native measured value or a fixed lookup from Tasks 8/9's saved evidence files.
- It does not re-derive Task 9's regime/session findings at runtime — they are hardcoded constants (`PREFERRED_SESSIONS`, `AVOID_SESSIONS`, etc. in `ios.py`) precisely so the "why" of every ranking stays traceable to a specific, citable number in `reports/edge_refinement/`.
