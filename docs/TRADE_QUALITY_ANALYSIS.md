# Trade Quality Analysis — S3 & S4 (Task 9)

Companion document to [`INSTITUTIONAL_EDGE_REFINEMENT_REPORT.md`](INSTITUTIONAL_EDGE_REFINEMENT_REPORT.md); this focuses specifically on trade-quality classification (winners / losers / near-misses / breakeven) and validates the Institutional Trade Quality Score (ITQS).

## Dataset

3,272 S3/S4 trades across 7 symbols (`reports/edge_refinement/master_feature_dataset.parquet`, 47 columns): 3,187 CLOSED, 85 REJECTED (by the risk gate, not by strategy logic). Of the CLOSED trades, classification by outcome:

- **Winners**: `realized_pnl > 0`
- **Losers**: `realized_pnl <= 0`
- **Near misses**: closed with `-0.3 <= r_multiple < 0` — lost, but only marginally (a specific research-only label, not used elsewhere in the platform)
- **Breakeven**: `|realized_pnl| < 1e-6`

## What separates winners from near-misses/losers

Per the Feature Importance analysis (Phase 2), the two features that consistently and significantly separate winners from losers on BOTH strategies are:

1. **Order Block freshness** (FRESH vs MITIGATED) — the OB referenced by the trade had never been touched vs. had already been touched/mitigated by the time of entry.
2. **Order Block quality score** — the platform's own OB quality metric (body size, wick ratio, displacement strength) is measurably higher among winners (S3: 0.313 vs 0.294, p=0.008; S4: 0.319 vs 0.306, p=0.005).

No other feature — session, regime, ATR, PDH/PDL distance, gap proximity, confidence score — reached statistical significance on both strategies simultaneously. This is the central, load-bearing finding of Task 9.

## ITQS validation

The Institutional Trade Quality Score (`src/research/itqs.py`, full formula in the main report's Phase 4) was built directly from this finding and validated the same way Task 8 validated the existing confidence score:

| Metric | Existing confidence score (Task 8) | ITQS (Task 9) |
| --- | --- | --- |
| Spearman corr. vs. r_multiple (S3) | ≈0.038 | **0.099** |
| Spearman corr. vs. r_multiple (S4) | ≈0.038 (combined-strategy figure) | **0.082** |
| Score variance across signals | **zero** (constant per strategy, see Phase 9) | Real, continuous distribution |
| Top-bucket vs bottom-bucket expectancy | N/A (no variance to bucket) | S3: 0.93R vs 0.24R; S4: 0.83R vs 0.20R |

ITQS is not a large improvement in absolute correlation terms (0.06-0.10 is still a modest effect size by conventional standards), but it is a **real, non-zero, validated signal** where the existing score has none — and its bucket-level separation (roughly 4x expectancy between top and bottom bucket) is large enough to be operationally useful as a research filter, even though the correlation coefficient itself is modest.

## Near-miss trades: a specific note

Near-miss trades (closed just short of breakeven, `-0.3R` to `0R`) were examined for whether they share characteristics with winners or losers. They did not show a distinct feature profile from ordinary losers in this analysis — the same Order Block freshness/quality pattern applies (near-misses skew toward MITIGATED/lower-quality OBs, same direction as full losers, not a distinct middle category). This is reported as a negative finding, not omitted: near-miss trades do not appear to be a separately identifiable population worth its own filter.

## Practical implication

Every alpha filter that survived Phase 10's evidence bar (18 of 32) either directly encodes or correlates with this Order Block freshness/quality finding. The institutional specifications for S3 and S4 (see the two dedicated specification documents) both add an explicit `ob_quality_score > median` gate on top of the existing `require_fresh_ob=True` requirement, precisely because Phase 3's confluence discovery showed this combination separates a 0.93R/0.74R-expectancy trade population from the 0.24R/0.20R baseline.
