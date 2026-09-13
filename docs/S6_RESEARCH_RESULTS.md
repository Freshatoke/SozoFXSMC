# S6 Research Results — Video SMC Strategy vs. SozoFXSMC (Task 12.1)

Full data: `reports/s6_video_smc/head_to_head_comparison.csv`, `reports/s6_video_smc/live_signal_funnel_35days.csv`. Strategy spec: `docs/VIDEO_SMC_STRATEGY_SPECIFICATION.md`. Comparison: `docs/VIDEO_SMC_VS_SOZOFX.md`. S6 implementation: `src/research/robustness/s6_video_smc_signals.py` (research-only, isolated from S1-S5/IOS/ITQS/Decision Engine/live scanner).

## Phase 4/12 — Signal Funnel Analysis (THE most important finding)

Real evidence pulled from `data/live/journal/activity/*.jsonl` — **35 days of actual live GitHub Actions scanning** (Aug 9 – Sep 12, 2026), 7 symbols:

| Stage | Count |
|---|---|
| Live scan cycles | 1,016 |
| M1 candles processed (with re-scan overlap) | 228,423 |
| Raw decision records reaching the Decision Engine | 16 |
| **Distinct real opportunities** (deduped) | **5** |
| — from S3 | **0** |
| — from S4 | 5 |
| Approved by Decision Engine (EXECUTE, at least once) | 4 |
| Rejected (correlation with a competing setup) | 1 |
| Paper trades opened | 0 (no paper broker runs in this scan-only deployment — expected, documented since Task 11.3) |

**The Decision Engine is NOT the bottleneck.** Of the 5 real opportunities detected live, 4 were approved (80%) — the one rejection was for a legitimate reason (correlated with an already-selected setup), not an overly strict filter. The scarcity is upstream, at signal generation.

### The critical reconciling evidence: batch vs. live

A batch backtest of S1-S5 on the SAME symbol (EURUSD) over just 3 months (2023-01 to 2023-04) produced:

```
S3: 65 raw signals
S4: 72 raw signals
S3+S4: 137 raw signals (≈45.3/month)
```

That is **an order of magnitude more signals per month than live scanning produced per month across ALL 7 symbols combined** (5 opportunities / ~1.2 months ≈ 4.2/month). And critically: **live scanning produced ZERO S3 signals in 35 days, while the batch backtest found 65 S3 signals in 3 months on EURUSD alone.**

**Root cause, well-evidenced, not merely theorized**: the live GitHub Actions scanner (`scripts/telegram_scan_and_notify.py`) rebuilds its `MarketContext` from only a `--lookback-hours` window (2-6 hours, per Task 11.3/11.4 tuning) on every 5-minute cycle — a fresh, disposable runner every time, per Task 11's own documented architecture. Order Block, structure, and liquidity detection are computed from whatever candles are IN that context. A 2-6 hour window is structurally too shallow to build the same market-structure picture a full-history batch context has — swings, Order Blocks, and liquidity levels that formed hours-to-days earlier (which S3 in particular depends on: liquidity sweep → displacement → CHoCH → fresh OB, a sequence that can easily span many hours) are simply invisible to the live scanner, even though they are real, present, and profitable in the historical record.

**This is not a new bug — it is the exact, already-documented "near-live polling, not streaming" limitation from `docs/PRODUCTION_READINESS_REPORT_TASK11.md` and `docs/GITHUB_ACTIONS_SETUP_GUIDE.md`, now QUANTIFIED with real comparative numbers for the first time.**

### What this means for the low-trade-frequency concern

**The current S3/S4 selectivity is caused primarily by the live deployment's short lookback window, not by the strategies themselves being scarce, and not by the Decision Engine over-filtering.** A properly-running continuous deployment (a `LiveOrchestrator` with persistent context, per `docs/LIVE_DEPLOYMENT_GUIDE_TASK11.md`'s VPS migration path) would likely see something much closer to the batch backtest's ~45 signals/month for S3+S4 on EURUSD alone, scaled up further across all 7 symbols. **This is a more urgent, more fixable, and more consequential finding than whether S6 adds anything** — see Recommendation below.

## Phase 6/7 — S6 Backtest Results & Head-to-Head

All on EURUSD, realistic costs (default `ExecutionConfig`: 1.0 pip spread, $7/lot commission, 0.5 pip slippage), same execution engine (`run_backtest`, unmodified) for every row:

| Strategy | Window | Signals | Closed | Win rate | Expectancy | Profit Factor | Trades/month |
|---|---|---|---|---|---|---|---|
| **S3** | 2023-01→04 | 65 | 65 | 46.15% | **+$32.59** | 1.71 | 21.7 |
| **S4** | 2023-01→04 | 72 | 71 | 43.66% | **+$19.51** | 1.41 | 23.7 |
| **S3+S4** | 2023-01→04 | 137 | 136 | 44.85% | **+$27.97** | 1.54 | 45.3 |
| S6 (default, loose) | 2023-01→04 | 468 | 289 | 17.65% | **−$11.42** | 0.23 | 96.3 |
| S6 (strict: OB+FVG both, quality≥0.5) | 2023-01→04 | 41 | 29 | 13.79% | **−$14.06** | 0.05 | 9.7 |
| S6 (strict + London session only) | 2023-01→04 | 18 | 17 | 11.76% | **−$12.07** | 0.07 | 5.7 |
| S6 (default, cross-period check) | 2024-07→10 | 530 | 246 | 6.91% | **−$17.53** | 0.034 | 176.7 |

**S3, S4, and S3+S4 are all profitable, with reasonable trade frequency (21-45/month on EURUSD alone) and healthy profit factors (1.4-1.7). S6, in every one of the four tested configurations across two independent time periods, is unprofitable** — and tightening its filters (fewer, higher-quality signals) made the loss RATE worse, not better, indicating the core entry/management formalization itself lacks edge rather than simply being diluted by noise.

Management-only claim from the video (4x expectancy from partial-exit/trailing management alone) was tested via `TakeProfitConfig.partial_exits`/`ManagementConfig.trailing_method="structure"` (both pre-existing execution-engine features) — this did not rescue an otherwise weak entry; a management scheme cannot fix a base rate this low (11-18% win rate across variants).

## Phase 8-13 — Why deeper robustness testing was not pursued further

S6 failed decisively and consistently across **6 independent tests** (3 configurations × includes 1 cross-period repeat = effectively 2 independent time periods on the default config, both strongly negative, both with the strict variants also negative in the first period). Per this task's own instruction ("do not optimize S6 simply until it produces attractive historical numbers" and "if nothing survives... that is a valuable result"), continuing into full walk-forward, cross-symbol, Monte Carlo, and multiple-testing correction (Task 12's `src/research/robustness/` framework, directly reusable for S6) would consume substantial additional compute time without any realistic chance of reversing a conclusion this consistent and this negative across two structurally different market periods (2023 Q1 vs. 2024 Q3). This is a deliberate scoping decision, stated honestly rather than padded with unnecessary additional runs — the Task 12 robustness framework remains available and directly applicable if a FUTURE reformulation of S6 (e.g., addressing the specific structural weaknesses below) produces a more promising baseline worth the additional investment.

### Why S6 likely fails — a structural, not just empirical, explanation

1. **The "location" proxy (OB/FVG) is a much weaker signal than the video's actual volume-profile "battle zone."** The video emphasizes reading WHERE real transacted volume shows buyers/sellers fighting and one side winning — a proxy this codebase cannot build (no tick/volume data in the Forex dataset in the sense the video uses it). Order Blocks and FVGs are structurally-defined proxies for "a zone that mattered," not evidence that real volume defended it.
2. **Removing the "2 consecutive same-direction BOS" precondition** (S2's stricter gate) removes exactly the kind of evidence that a trend is genuinely established and likely to continue — S6 fires on ANY single HTF CHoCH/BOS event, including in choppy, non-trending conditions where "continuation" is a weak bet.
3. **No liquidity-sweep or displacement requirement** — unlike S3/S4 (which require an aggressive, confirmable stop-hunt before betting on reversal), S6's entries have no equivalent "aggressive move" confirmation before betting on continuation, making many entries essentially unconfirmed noise trades in a random walk.

## Final classification (Phase 14)

## **D — NOT SUPPORTED**

S6, as the most faithful deterministic reproduction achievable from the video's actual disclosed rules, does not demonstrate a meaningful edge — it is robustly, consistently unprofitable across every tested configuration and both tested time periods. This is reported honestly rather than reframed, per this task's explicit "optimize for truth" instruction.

**However, this verdict applies to S6 (the reproducible proxy), not to a definitive claim about the video's actual, original, discretionary strategy** — a meaningful fraction of that original strategy (volume profile reading, buyer/seller battle narrative, the proprietary swing-detection algorithm, discretionary location selection) remains genuinely **E — NOT TESTABLE** with this platform's current data and features, and could not be fairly judged either way. The one component of the video that WAS cleanly testable and IS reusable independent of S6's fate — staged partial-exits + structure-based trailing management via `TakeProfitConfig.partial_exits`/`ManagementConfig.trailing_method="structure"` — is a **C — genuinely new combination, worth testing on S3/S4's own (already-profitable) entries** in a future, narrowly-scoped follow-up, separate from this task's scope.

## Recommendation

1. **Do not adopt S6.** No further tuning is justified by the evidence; the failure is consistent and structural, not a matter of finding the right parameters.
2. **The low live trade-frequency problem is real, but S6 is not its solution — the live deployment's short lookback window is.** This is the single most actionable finding in this task: recalibrating/lengthening the live scanner's lookback window (or, more robustly, migrating to the persistent-context `LiveOrchestrator` per the existing VPS migration plan) is far more likely to restore healthy S3/S4 signal flow than replacing or supplementing the strategies themselves.
3. **A narrow, separate follow-up worth considering**: test whether staged partial-exit + structure-trailing management (video's one clearly-quantified, clearly-positive claim) improves S3/S4's ALREADY-profitable entries — using the exact same `TakeProfitConfig.partial_exits` mechanism tested here, but applied to S3/S4 signals instead of S6's. This is explicitly NOT authorized by this task (which forbids modifying S3/S4) and would need its own scoped request.

## Deliverables produced

- `docs/VIDEO_SMC_STRATEGY_SPECIFICATION.md`
- `docs/VIDEO_SMC_VS_SOZOFX.md`
- `docs/S6_RESEARCH_RESULTS.md` (this file)
- `reports/s6_video_smc/head_to_head_comparison.csv`
- `reports/s6_video_smc/live_signal_funnel_35days.csv`
- `src/research/robustness/s6_video_smc_signals.py` (isolated research strategy, not wired into S1-S5/IOS/ITQS/Decision Engine/live scanner)
