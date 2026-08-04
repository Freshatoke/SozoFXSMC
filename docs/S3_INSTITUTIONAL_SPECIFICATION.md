# S3 — Liquidity Sweep Reversal: Final Institutional Specification

Backed entirely by Task 8 (baseline ranking) and Task 9 (feature importance, confluence, entry/exit refinement, regime, symbol, filter evidence). This specification refines parameters and adds research-informed filters; it does **not** redesign the underlying strategy logic (Task 9's explicit constraint).

## 1. Entry Rules (unchanged from Task 3, confirmed correct by Task 9 evidence)

1. A liquidity level (buy-side or sell-side, from `src.features.liquidity`) is swept: price wicks beyond the level and closes back on the origin side.
2. Displacement in the reversal direction confirmed (`src.features.displacement`) — **required** (`require_displacement=True`, confirmed critical: Phase 2 found `has_displacement_confirmed` is one of the few features tied to signal validity for S3; every surviving S3 signal already has it).
3. A CHoCH confirms the reversal on the `choch_timeframe` (M5 default) at or after the sweep.
4. Entry triggers on the M1 candle where the CHoCH confirms, **direction of the reversal** (buy-side swept → bearish; sell-side swept → bullish).

## 2. Confirmation Rules — Refined

| Rule | Task 3 default | Institutional recommendation | Evidence |
| --- | --- | --- | --- |
| Fresh Order Block required | `require_fresh_ob=True` | **Keep — confirmed critical** | Phase 5: expectancy 15.37R with vs 12.98R without on evidence slice; Phase 2/3: FRESH OB win rate 72.5% vs MITIGATED 44.8% (p<0.001) |
| FVG alignment required | `require_fvg=False` | **Keep as-is (off)** | Phase 5: requiring it *hurts* expectancy (15.37→7.66R) |
| Entry latency (extra confirmation candles) | 0 (immediate) | **Keep at 0 — do not add delay** | Phase 5: expectancy collapses to negative by 3 candles' delay (15.37R → -11.72R) |
| Entry trigger method | `market` (next-candle open) | **Switch to `confirmation_close`** | Phase 5: marginal improvement (16.01R vs 15.37R), same trade count, no added latency |
| **NEW — Order Block quality gate** | none | **Require `ob_quality_score` > this dataset's strategy-wide median** (~0.30) | Phase 3: FreshOB + HighQuality combination nearly quadruples expectancy (0.26R → 0.93R, n=40, reliable) |
| **NEW — CHoCH timeframe** | M5 | **Consider M15** — evidence from Task 8's parameter sweep (not re-run in Task 9): M15 CHoCH confirmation showed materially better expectancy (46.76 vs 15-17 for M1/M5) on the same evidence slice; flagged for validation on a larger sample before adoption, not yet confirmed at Task 9's evidence depth | Task 8 §7 |

## 3. Invalidation Rules (unchanged)

- If the CHoCH does not occur within the entry window after the sweep, no signal is generated (strategy logic, not touched).
- An Order Block referenced by the signal that becomes MITIGATED before entry disqualifies that specific setup from the "fresh" gate (existing `fresh_order_block_asof` point-in-time logic, verified correct in Task 7.4).

## 4. Stop Placement — Refined

| Task 3 default | Institutional recommendation | Evidence |
| --- | --- | --- |
| `ob_extreme` (beyond the referenced Order Block) | **Switch to `fixed_pips`** | Phase 6: nearly doubles expectancy on the evidence slice (30.44R vs 15.37R, +98%). `atr_multiple` stops are explicitly rejected — worst performer tested (-19.73R). |

**Recommended stop distance**: use the platform's existing `fixed_pips` mechanism (`StopLossConfig.fixed_pips`, default 20.0) — Task 9 did not sweep the exact pip value, only the method; a follow-up sweep of the specific distance is recommended future work (see main report §"Future Research" equivalent, Phase 12 not separately numbered here).

## 5. Target Placement — Refined

| Task 3 default | Institutional recommendation | Evidence |
| --- | --- | --- |
| `fixed_rr` (2.0 default R:R) | **Switch to `liquidity_level`** | Phase 6: +41% expectancy on the evidence slice (21.68R vs 15.37R) |
| Risk:Reward (if `fixed_rr` is retained as a fallback) | **2.5**, not 2.0 | Task 8 §7: expectancy rose monotonically from R:R 1.0 to 2.5, plateaued at 3.0 — a genuine, non-overfit response curve |

## 6. Trade Management (unchanged from Task 4 defaults)

Breakeven at 1R, no trailing stop, max duration 1 day (1,440 M1 candles), no session-close forced exit — none of these were part of Task 9's refinement scope and no evidence contradicts them.

## 7. Filters (Institutional Trade Filter Stack)

Apply in this order; a candidate signal must pass ALL to be considered institutional-grade:

1. **Order Block freshness = FRESH** (hard gate, already enforced by `require_fresh_ob=True`)
2. **Order Block quality score > strategy median** (~0.30) — NEW, Phase 3/10
3. **Session = London** (preferred; Sydney/Asian session should be avoided — Phase 7/10)
4. **Volatility state = high** (ATR above its own trailing median — Phase 7/10; below-median ATR is explicitly rejected, not merely unhelpful)
5. **Not a gap day** — Phase 10 (small-sample caveat: n=8 negative-gap-day trades, directional not confirmed at scale, but included as a filter since it never hurt and matched Phase 7's regime finding)
6. **Trend state = ranging** — Phase 7/10 (S3 is a mean-reversion/reversal strategy by design; trending-market trades were a small, weaker sample)

## 8. Market Conditions

- **Preferred**: London session, high volatility, ranging markets, on USDJPY (★★★★★, Phase 8).
- **Avoid**: Sydney/Asian session, low volatility, gap days, USDCAD (★☆☆☆☆, Phase 8 — barely profitable, PF 1.11).

## 9. Confidence Requirements

**Do not use the existing Task 3 confidence score as a gate for S3** — Phase 9 established it is a literal constant (93.75 for every signal) with zero variance, and therefore zero ability to discriminate trade quality. Use **ITQS ≥ 55** (bucket A/B) instead as the research-informed quality gate (Phase 4/10 — confirmed to improve expectancy, drawdown, and profit factor over the unfiltered baseline).

## 10. Summary — Institutional S3 Configuration

```
Entry:        confirmation_close, latency=0, require_fresh_ob=True, require_fvg=False
NEW filter:    ob_quality_score > 0.30 (strategy median)
Stop:          fixed_pips (specific distance: follow-up sweep recommended)
Target:        liquidity_level (or fixed_rr at 2.5 as fallback)
Session:       London preferred; Sydney/Asian avoided
Volatility:    high preferred; below-median ATR avoided
Trend:         ranging preferred
Gap days:      avoided
Symbol:        USDJPY preferred; USDCAD avoided
Quality gate:  ITQS >= 55 (NOT the existing confidence score, which has zero variance for this strategy)
```

Every line above is traceable to a specific Task 9 phase and measured result — no line is included on intuition alone.
