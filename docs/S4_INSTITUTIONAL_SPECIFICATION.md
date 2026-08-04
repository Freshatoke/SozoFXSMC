# S4 — Previous Day High/Low Sweep: Final Institutional Specification

Backed entirely by Task 8 (baseline ranking) and Task 9 (feature importance, confluence, entry/exit refinement, regime, symbol, filter evidence). This specification refines parameters and adds research-informed filters; it does **not** redesign the underlying strategy logic.

## 1. Entry Rules (unchanged from Task 3, confirmed correct by Task 9 evidence)

1. Previous Day High (PDH) or Previous Day Low (PDL) is swept: price wicks beyond the level and closes back on the origin side.
2. A CHoCH confirms the reversal on the `choch_timeframe` (M5 default) at or after the sweep.
3. Entry triggers on the M1 candle where the CHoCH confirms (PDH swept → bearish reversal; PDL swept → bullish reversal).

Note: S4 does not reference the generic liquidity-level engine's IDs the way S3 does (it sweeps PDH/PDL reference levels directly) — this is a legitimate structural difference between the two strategies, confirmed in Phase 10 (liquidity-strength filters were untestable for S4, not merely unhelpful — zero matching data).

## 2. Confirmation Rules — Refined

| Rule | Task 3 default | Institutional recommendation | Evidence |
| --- | --- | --- | --- |
| Fresh Order Block required | `require_fresh_ob=True` | **Keep — confirmed even more critical than for S3** | Phase 5: expectancy 13.78R with vs **4.23R without (3.3x difference)** — the single largest entry-refinement effect measured in this task; Phase 2/3: FRESH OB win rate 71.2% vs MITIGATED 45.7% (p<0.001) |
| FVG alignment required | `require_fvg=False` | **Keep as-is (off)** | Phase 5: requiring it hurts expectancy (13.78→10.24R) |
| Entry latency (extra confirmation candles) | 0 (immediate) | **Keep at 0**, though 1-candle delay was marginally better in this evidence slice (14.97R vs 13.78R) | Phase 5: difference is small and not judged reliable enough to override the simpler zero-latency default; noted as a candidate for a larger-sample follow-up, not adopted here |
| Entry trigger method | `market` (next-candle open) | **Keep — confirmed best of the four methods tested** | Phase 5: market entry outperformed confirmation_close, ob_touch, and ob_proximal_edge |
| **NEW — Order Block quality gate** | none | **Require `ob_quality_score` > this dataset's strategy-wide median** (~0.31) | Phase 3: FreshOB + HighQuality combination more than triples expectancy (0.22R → 0.74R, n=73, reliable) |

## 3. Invalidation Rules (unchanged)

Same point-in-time Order Block mitigation logic as S3 (§3 of the S3 spec) — verified correct in Task 7.4, not touched by Task 9.

## 4. Stop Placement — Confirmed, Not Changed

| Task 3 default | Institutional recommendation | Evidence |
| --- | --- | --- |
| `ob_extreme` | **Keep — confirmed best of four methods tested** | Phase 6: 13.78R vs m5_structural (-1.23R), atr_multiple (-35.65R, worst), fixed_pips (0.85R). Unlike S3, S4's default stop method is already optimal among the alternatives. |

## 5. Target Placement — Confirmed, Not Changed

| Task 3 default | Institutional recommendation | Evidence |
| --- | --- | --- |
| `fixed_rr` (2.0 default) | **Keep the method**; consider raising R:R toward **2.5** | Phase 6: fixed_rr (13.78R) outperformed previous_high_low, liquidity_level, and next_bos_target for S4. Task 8 §7's R:R sweep (all strategies, not S4-specific) found 2.5 the best-performing value on the same evidence slice — apply the same reasoning here pending an S4-specific confirmation sweep. |

## 6. Trade Management (unchanged from Task 4 defaults)

No changes supported by Task 9 evidence — same as S3 §6.

## 7. Filters (Institutional Trade Filter Stack)

Apply in this order; a candidate signal must pass ALL to be considered institutional-grade:

1. **Order Block freshness = FRESH** (hard gate — the single most important rule for S4, Phase 5)
2. **Order Block quality score > strategy median** (~0.31) — NEW, Phase 3/10
3. **Session = Tokyo** (preferred, not merely acceptable — Phase 7: weighted expectancy 161.2 on n=344, the largest reliable regime effect found for either strategy); London is a solid secondary session (71.6 weighted expectancy)
4. **Volatility state = high** — Phase 7/10 (below-median ATR explicitly rejected)
5. **Order Block age below median** (younger OB) — Phase 10, accepted filter, improves both expectancy and profit factor

## 8. Market Conditions

- **Preferred**: Tokyo session (primary), London (secondary), high volatility, on NZDUSD/USDJPY/GBPUSD (all ★★★★☆, Phase 8).
- **Avoid**: AUDUSD, USDCHF (both ★☆☆☆☆, profit factor < 1.32 — weakest of the 7 symbols tested).
- Unlike S3, **no regime was found where S4 was outright unprofitable** on a reliable sample — this is a genuine strength distinguishing S4 from S3, not an oversight.

## 9. Confidence Requirements

**Do not use the existing Task 3 confidence score as a gate for S4** — Phase 9 established it is a literal constant (92.31 for every signal) with zero variance. Use **ITQS ≥ 55** (bucket A/B) instead (Phase 4/10).

## 10. Summary — Institutional S4 Configuration

```
Entry:        market, latency=0, require_fresh_ob=True (critical, 3.3x expectancy effect), require_fvg=False
NEW filter:    ob_quality_score > 0.31 (strategy median)
Stop:          ob_extreme (confirmed best of alternatives -- no change)
Target:        fixed_rr (confirmed best of alternatives); consider R:R=2.5 pending S4-specific confirmation
Session:       Tokyo preferred, London secondary
Volatility:    high preferred; below-median ATR avoided
OB age:        younger (below median) preferred
Symbol:        NZDUSD / USDJPY / GBPUSD preferred; AUDUSD / USDCHF avoided
Quality gate:  ITQS >= 55 (NOT the existing confidence score, which has zero variance for this strategy)
```

Every line above is traceable to a specific Task 9 phase and measured result — no line is included on intuition alone. S4 required fewer changes than S3: its Task 3 defaults for stop and target were already the best-performing options among every alternative tested, and its dependence on Order Block freshness is even stronger than S3's.
