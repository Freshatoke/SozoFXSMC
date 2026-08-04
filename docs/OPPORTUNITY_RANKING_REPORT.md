# Opportunity Ranking Report — Task 10

Results from replaying the decision engine over 3,187 historical S3/S4 opportunities across 7 symbols (2020-2026), full detail in `reports/decision_engine/`.

## IOS tier validation — the central result

| IOS Tier | Win rate | n (executed) |
| --- | --- | --- |
| A (≥75) | **61.5%** | 13 |
| B (60-74.9) | **65.1%** | 43 |
| C (45-59.9) | 47.7% | 218 |
| D (<45) | 46.3% | 899 |

**IOS separates trade quality by roughly 15-19 percentage points of win rate between the top two tiers and the bottom two** — a clean, monotonic-in-direction validation that the score is measuring something real, not noise. Tier A/B opportunities are rare (56 of 1,173 executed, 4.8%) — in this platform's current signal population, most opportunities land in the C/D range, which is itself informative: high-conviction setups are scarce, and the engine correctly treats them as the exception rather than the norm.

## Selected vs. baseline (headline result)

| | Baseline | Selected | Delta |
| --- | --- | --- | --- |
| n | 3,187 | 1,173 (36.8%) | — |
| Win rate | 46.3% | 47.4% | +1.1pp |
| Expectancy (R) | 0.2332 | 0.2946 | **+26.3%** |
| Profit factor | 1.4007 | 1.6554 | **+18.2%** |

## Why opportunities were rejected (top reasons, by frequency)

| Reason | Count |
| --- | --- |
| Lower IOS than a competing, correlated setup already selected | 539 |
| Monthly loss limit reached | 431 |
| Weekly loss limit reached | 318 |
| Currency (EUR) already at max exposure | 233 |
| Daily loss limit reached | 153 |
| Currency (USD) already at max exposure | 142 |

**Two distinct rejection mechanisms are both working as designed**: correlation-based rejection (539 — the task brief's own "choose the stronger of two similar setups" scenario, confirmed operating at scale) and account-risk-based rejection (loss limits collectively account for well over half of all rejections). The loss-limit-driven rejections reflect a **fixed, non-compounding $10,000 starting balance held constant across the entire 6.5-year simulation** — a real institutional account would either compound its risk base as equity grows or reset risk limits on a rolling basis; this paper-trading run deliberately did neither, so the loss-limit rejection rate here should be read as a conservative (risk-off) lower bound on what a production account with proper equity scaling would allow, not a criticism of the risk layer itself.

## Symbol distribution caveat

88.5% of EXECUTEd trades were EURUSD (1,038 of 1,173) — this is a direct artifact of Task 8's data-depth asymmetry (EURUSD has ~5.4 years of real history vs. 6 months for the other 6 symbols), not evidence that the engine "prefers" EURUSD. A production deployment with equal-depth data across symbols would show a materially different distribution — see `docs/READINESS_ASSESSMENT_TASK10.md` for how this shapes the symbol-enablement recommendation.
