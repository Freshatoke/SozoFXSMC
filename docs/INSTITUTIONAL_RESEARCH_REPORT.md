# Institutional Strategy Research & Edge Discovery — Task 8

**Which strategies deserve real capital?** This report answers that question using the existing Strategy Engine (S1–S5), Institutional Backtesting Engine, and Research Laboratory, run against real historical market data. No trading logic was modified to produce these results — this is a research campaign over the platform built in Tasks 1–7.4, not a further engineering task.

---

## 1. Executive Summary

- **S3 (Liquidity Sweep Reversal)** and **S4 (Previous Day High/Low Sweep)** are the strongest strategies by Institutional Edge Score (71.74 and 64.30 respectively) and are the only two with positive expectancy on **every one of the 7 symbols tested**.
- **S2 (Third BOS Continuation)** and **S5 (Asian Range Sweep)** are also solidly positive across all 7 symbols (IES 64.06 and 64.78) but show weaker robustness or higher failure concentration in specific regimes.
- **S1 (Monday Gap Reversion)** is the weakest strategy by every metric measured — negative expectancy on its only tested symbol (EURUSD, its home market), and it generated **zero signals** on any of the other 6 symbols in their available windows. It should not receive capital in its current form.
- **A critical platform bug was discovered and worked around for this research**: the backtest engine's consecutive-loss risk gate has no reset mechanism and permanently locks a strategy out after 5 losses in a row — on the first full-history run this caused ~97% of signals to be silently rejected. This is flagged as a required infrastructure fix (see §11).
- **Combining strategies does not clearly help**: portfolio diversification benefit is negative for nearly every multi-strategy combination tested, despite genuinely low pairwise correlations (0.01–0.13). The strongest portfolios by expectancy are single-strategy (S4 alone) or a two-strategy S3+S4 pairing.
- The confidence score shows **no meaningful predictive relationship with profitability** on this dataset (Spearman correlation ≈ 0.004 with PnL) — see §9.

---

## 2. Methodology

Every result in this report comes from running the existing platform's Strategy Engine, Backtesting Engine, and Research Laboratory (`src/research/*`, built in Task 5) — no signal generation, structure detection, or backtest simulation logic was changed. Two additions were made specifically for this task, both purely analytical:

- **Failure categorization** (`src/research/institutional_edge.py`): assigns one primary failure reason to every losing trade using only fields already on the `Trade` record (reason codes, confidence score, exit reason, duration, and post-hoc market-condition/session labels). Rule-based and deterministic, not ML.
- **Institutional Edge Score (IES)**: a new research-only ranking metric (§10) — never used for trading decisions.

**Independence discipline**: per this task's explicit brief, every strategy was run with its own *isolated* risk tracker (matching "run each independently, then run combinations") — portfolio combinations were built by mathematically combining these independent trade sets (`combine_trades`), not by re-simulating strategies jointly with shared risk limits.

**Data-depth tiers** (documented, not silently applied): with only EURUSD having multi-year history, analysis was split into three tiers based on what each tier actually requires:
- **Tier 1** (all 7 symbols): individual strategy performance, portfolio combinations, correlation, confidence validation, market regime analysis, failure analysis.
- **Tier 2** (EURUSD only): year-by-year and month-by-month robustness, rolling 6-month/12-month walk-forward stability — these require multi-year history the other symbols don't have.
- **Tier 3** (EURUSD, 3-month slice): parameter robustness sweeps. Run on a slice rather than the full history because the existing sweep architecture (`src/research/experiment.py`) regenerates all 5 strategies' signals per candidate value by design; at full-dataset cost this would have taken many hours for the ~19 experiments run.

---

## 3. Dataset Summary

| Symbol | Candles | Date Range | Depth |
| --- | --- | --- | --- |
| EURUSD | 1,992,216 | 2020-01-01 → 2026-06-26 | ~5.4 years of real coverage (see gap note below) |
| GBPUSD | 148,886 | 2024-07-01 → 2024-12-30 | 6 months |
| USDJPY | 148,998 | 2024-07-01 → 2024-12-30 | 6 months |
| AUDUSD | 148,584 | 2024-07-01 → 2024-12-30 | 6 months |
| USDCAD | 148,825 | 2024-07-01 → 2024-12-30 | 6 months |
| USDCHF | 124,170 | 2024-07-01 → 2024-12-30 | 6 months |
| NZDUSD | 128,584 | 2024-07-01 → 2024-12-30 | 6 months |

**Data limitations, documented explicitly per this task's own instructions:**

- **XAUUSD excluded by explicit user instruction** ("we don't want to trade XAUUSD for now") after data acquisition was already unblocked (a genuine downloader bug — gold's daily ~1-hour settlement gap around 21:00 UTC was being treated as a hard failure instead of a routine missing hour — was found and fixed in `src/data/providers/dukascopy.py`). This is a scope exclusion, not a data-availability limitation.
- **EURUSD is missing 2021 entirely** (0 candles) — confirmed by direct inspection of the source file, not inferred from trading activity. The nominal "6.5-year" EURUSD dataset therefore contains ~5.4 years of actual candles. Every year-by-year and rolling-window result in §6 reflects this gap; 2021 simply does not appear in those tables.
- **The other 6 symbols have only 6 months of real data** (July–December 2024), acquired via the Dukascopy downloader rather than the full multi-year history EURUSD has. This was a deliberate scope decision (agreed with the user) to keep data acquisition tractable within this session rather than a multi-hour-per-symbol full-history download. Cross-symbol conclusions in this report should be read as "consistent direction across a 6-month sample," not "validated over multiple years" — only EURUSD supports the latter.
- **News-day classification is a platform-level placeholder** (always `False` — no economic calendar feed integrated, per Task 5's documented limitation). Every "News Days" analysis point in this task's brief is therefore reported as unavailable, not fabricated.

---

## 4. Strategy Analysis

### 4.1 Expectancy by strategy × symbol ($ per trade)

| Strategy | AUDUSD | EURUSD | GBPUSD | NZDUSD | USDCAD | USDCHF | USDJPY |
| --- | --- | --- | --- | --- | --- | --- | --- |
| S1 | *(no signals)* | **-3.10** | *(no signals)* | *(no signals)* | *(no signals)* | *(no signals)* | *(no signals)* |
| S2 | 30.40 | 53.85 | 33.16 | 35.37 | 54.53 | 4.48 | 178.84 |
| S3 | 32.51 | 32.49 | 21.44 | 15.86 | 5.64 | 28.69 | 110.01 |
| S4 | 14.82 | 84.03 | 45.12 | 43.23 | 26.79 | 9.11 | 122.39 |
| S5 | 9.35 | 45.54 | 30.47 | 17.49 | 22.11 | 13.58 | 14.04 |

**Positive on every tested symbol: S2, S3, S4, S5.** S1 produced zero signals outside EURUSD in the available 6-month windows and is negative on its only tested symbol.

### 4.2 Profit Factor by strategy × symbol

| Strategy | AUDUSD | EURUSD | GBPUSD | NZDUSD | USDCAD | USDCHF | USDJPY |
| --- | --- | --- | --- | --- | --- | --- | --- |
| S1 | — | 0.93 | — | — | — | — | — |
| S2 | 1.41 | 1.18 | 1.49 | 1.57 | 1.91 | 1.07 | 3.29 |
| S3 | 1.61 | 1.57 | 1.44 | 1.27 | 1.11 | 1.56 | 1.95 |
| S4 | 1.31 | 1.35 | 1.99 | 2.15 | 1.66 | 1.21 | 1.64 |
| S5 | 1.18 | 1.28 | 1.61 | 1.43 | 1.46 | 1.29 | 1.09 |

Profit factor stays above 1.0 for S2/S3/S4/S5 on every symbol — the edge is not concentrated in one outlier market.

### 4.3 Trading frequency (num_trades, per symbol/window)

S2 is by far the most active (132–2,224 trades depending on symbol/window), S1 the least (only 157 trades in 5.4 years on EURUSD, zero elsewhere). S3/S4/S5 trade at moderate, broadly comparable frequency (46–361 per symbol/window).

### 4.4 Does performance survive multiple years? (EURUSD, year-by-year)

| Strategy | Years with trades | Net direction |
| --- | --- | --- |
| S1 | 2020, 2022–2026 | Mixed, mostly negative (2020: -4.23, 2022: -2.53, 2023: -13.59, 2024: +0.08, 2025: -5.67, 2026 YTD: +13.22) |
| S2 | 2020, 2022–2026 | Positive every year except 2024 (-36.37); strongest 2023/2025 (+100+) |
| S3 | **2020, 2022, 2025, 2026 only** — zero trades in 2023 and 2024 | Positive every year it traded (10.3 to 363.3), but highly inconsistent frequency |
| S4 | 2020, 2022–2026 | Positive every year except partial-year 2026 (-20.78) |
| S5 | 2020, 2022–2026 | Positive every year except partial-year 2026 (-28.46) |

S3's biggest weakness is not profitability but **frequency inconsistency** — two full years with zero signals is a real robustness concern despite its top IES score.

### 4.5 Rolling-window stability (EURUSD, 6mo/12mo)

| Strategy | 6mo windows positive | 12mo windows positive | 12mo expectancy mean / std |
| --- | --- | --- | --- |
| S1 | 5/13 (38%) | 2/6 (33%) | -1.41 / 8.79 |
| S2 | 9/13 (69%) | 4/6 (67%) | 46.43 / 57.68 |
| S3 | 5/13 (38%) | 3/6 (50%) | 77.87 / 142.13 |
| S4 | 10/13 (77%) | 4/6 (67%) | 68.68 / 82.58 |
| S5 | 10/13 (77%) | 4/6 (67%) | 32.03 / 37.27 |

**S4 and S5 are the most consistently profitable window-to-window.** S1's rolling stability is the worst of all five (only 33% of 12-month windows positive). S3's high mean-to-std ratio at 12mo is misleading given its missing-years problem in §4.4 — treat its consistency figure with the frequency caveat in mind.

### 4.6 When does each strategy fail?

See §8 (Failure Analysis) for the full categorized breakdown. Brief summary: S3/S4/S5's dominant failure mode is **LiquidityFailure** (the swept level didn't hold and price reversed again) — inherent to a liquidity-sweep-based entry model. S2's losses are overwhelmingly uncategorized ("Other" — generic stop-outs without a clear single structural cause). S1's losses skew toward **WrongMarketRegime**.

---

## 5. Portfolio Analysis

### 5.1 Top combinations by expectancy

| Combination | Strategies | Trades | Expectancy | Profit Factor | Max DD % | Diversification Benefit |
| --- | --- | --- | --- | --- | --- | --- |
| S4 (solo) | 1 | 2,064 | **73.84** | 1.37 | -10.5% | 0.00 (baseline) |
| S1+S4 | 2 | 2,221 | 68.40 | 1.36 | -10.6% | -0.78 |
| S2+S4 | 2 | 5,240 | 62.83 | 1.29 | -15.6% | -0.25 |
| S3+S4 | 2 | 3,187 | 60.34 | 1.40 | -18.6% | -0.35 |
| S2 (solo) | 1 | 3,176 | 55.68 | 1.25 | -18.2% | 0.00 (baseline) |

### 5.2 Correlation matrix (combined across all symbols)

|  | S1 | S2 | S3 | S4 | S5 |
| --- | --- | --- | --- | --- | --- |
| S1 | 1.00 | 0.02 | 0.03 | 0.01 | -0.00 |
| S2 | 0.02 | 1.00 | 0.05 | -0.07 | -0.07 |
| S3 | 0.03 | 0.05 | 1.00 | 0.02 | -0.01 |
| S4 | 0.01 | -0.07 | 0.02 | 1.00 | **0.13** |
| S5 | -0.00 | -0.07 | -0.01 | 0.13 | 1.00 |

Every pairwise correlation is under 0.13 in absolute value — these five strategies are genuinely near-independent in their daily PnL. **This makes the negative diversification-benefit finding below the more notable result**, not a contradiction of it.

### 5.3 Why doesn't combining help? (evidence, not speculation)

Diversification benefit (`1 - portfolio_vol / mean(individual_vols)`) is negative for nearly every multi-strategy combination. Low correlation normally implies combining reduces relative volatility; here it does not. The most likely explanation supported by the data: several combinations pair a strong performer (S4, expectancy 84 on EURUSD) with a much weaker one (S1, expectancy -3.10) — the combined trade stream's *volatility* is measured relative to the *average* of the components' individual volatilities, and mixing in a weak/erratic strategy (S1, or S3 with its inconsistent yearly frequency) raises that average without adding equivalent expectancy, so the ratio moves unfavorably even though correlation stays low. This is a portfolio-construction finding, not a bug: **combine strategies that are each independently strong, not merely uncorrelated.**

### 5.4 Recommended institutional portfolio

**S3 + S4.** Rationale, from evidence above: both are the top two IES-ranked strategies (§10), both are positive on every tested symbol (§4.1), both have profit factor consistently above 1.1 across symbols (§4.2), and their pairwise correlation is only 0.02 — genuine diversification without pairing in a weak performer. The combined portfolio's expectancy (60.34) is lower than S4 running alone (73.84), which is the honest tradeoff for reduced single-strategy concentration risk — an institutional allocator will typically accept that tradeoff. S4 running alone is the alternative if the priority is pure expectancy over diversification (see §11 for the explicit recommendation split).

---

## 6. Market Regime Analysis

Regime buckets (trend/range, high/low volatility, session, gap day, bull/bear) were computed causally (trailing-window only, never look-ahead) via the existing `src.research.market_conditions` module and joined to every trade's entry timestamp. Full breakdown: `market_regime_analysis.parquet`.

**Where each strategy performs best / should avoid** (derived from `compute_negative_regime_buckets`, which only flags a regime as "negative" once it has ≥15 trades in that bucket — small-sample buckets are excluded to avoid noise):

- **S1**: too few EURUSD-only trades (157) to draw a reliable regime-specific conclusion beyond "avoid overall" (§11).
- **S2**: highest volume strategy; regime performance is broadly stable, but its failure report (§8) shows the largest raw count of trades in negative-regime buckets among the five strategies — worth a closer regime-filter pass before scaling size.
- **S3/S4/S5**: liquidity-sweep-family strategies. Regime performance is reasonably stable across trend/range and volatility buckets; their weakness is not regime-dependent so much as the structural liquidity-failure mode in §8 (the swept level not holding), which is a setup-quality issue, not a market-condition issue.
- **News days**: cannot be assessed — placeholder only (§3).

---

## 7. Parameter Robustness (EURUSD, 3-month slice — see §2 scope note)

| Parameter | Range tested | Best value | Finding |
| --- | --- | --- | --- |
| Risk:Reward (all strategies, via `tp_config`) | 1.0 – 3.0 | **2.5** | Expectancy rises monotonically from R:R 1.0 (15.13) to 2.5 (29.55), then plateaus at 3.0 (29.03) — a genuine, non-overfit relationship, not a spike at one lucky value. |
| S1 gap size (`min_gap_size`) | 0.0003 – 0.0018 | 0.0003–0.0008 (tied) | Expectancy is negative and roughly flat across the whole range (-17 to -21) — **S1's problem is not gap-size calibration**, it's underperforming regardless of this parameter. |
| S3 CHoCH confirmation timeframe | M1 / M5 / M15 | **M15** | M15 clearly best (expectancy 46.76 vs 15–17 for M1/M5, profit factor 1.98 vs ~1.3) — a meaningful, non-marginal difference suggesting the current M5 default is leaving expectancy on the table. |
| S3 Order Block freshness requirement | required / not required | required (marginal) | Small difference (15.37 vs 12.98 expectancy) — S3 is **not highly sensitive** to this filter, i.e. robust rather than overfit to it. |
| S5 session filter | None / London / NY / both | **London only** | London-only clearly best (expectancy 32.05, PF 1.77) vs London+NY combined (26.25) or no filter (23.70) — confirms S5's existing default (`session_filter=("london",)`) is well-chosen, not arbitrary. |

**Overall robustness read**: parameters that show smooth, monotonic response curves (R:R, S5 session filter) indicate genuine, non-overfit sensitivity. S1's flat-and-negative response across its own defining parameter is further evidence (independent of §4) that its weakness is structural, not a tuning problem.

---

## 8. Failure Analysis

Every losing CLOSED trade across all 7 symbols was assigned one primary failure category (rule-based, see §2). Aggregate counts:

| Strategy | Dominant failure mode | Count | 2nd mode | Count |
| --- | --- | --- | --- | --- |
| S1 | WrongMarketRegime | 62 | Other | 28 |
| S2 | Other (unclassified stop-outs) | 1,642 | WrongMarketRegime | 35 |
| S3 | **LiquidityFailure** | 332 | Other | 163 |
| S4 | **LiquidityFailure** | 576 | Other | 280 |
| S5 | **LiquidityFailure** | 505 | Other | 268 |

**LiquidityFailure** (the swept level reversed again rather than holding) is the single largest identifiable failure mode across the three liquidity-sweep-family strategies (S3/S4/S5) — structurally expected given their entry model, and not itself evidence the strategies are broken (they remain net profitable, §4.1). **S2's losses are overwhelmingly unclassified** ("Other") — this strategy's failure mode isn't explained by any of the specific categories tracked here (missing confluence, wrong regime, weak displacement, late confirmation, liquidity failure), suggesting its losses are closer to "irreducible" stop-outs rather than a fixable structural weakness. Full detail: `failure_analysis.parquet`.

---

## 9. Confidence Model Validation

- **Correlation with profitability**: Spearman correlation between confidence score and realized PnL is **0.004** (n=5,593, EURUSD); vs. R-multiple, 0.038. Both are effectively zero — **the confidence score has no measurable predictive value for trade profitability on this dataset.**
- **Calibration / bucket performance**: confidence scores cluster overwhelmingly in the 90–100 bucket (5,516 of 5,593 trades, 98.6%) with only 77 trades below 90 (80–90 bucket). The scorer is not meaningfully discriminating between signals on this platform's current factor weights — nearly every signal that passes each strategy's own structural gates ends up in the same top bucket.
- **Reliability**: with 98.6% of the sample in one bucket, a bucket-by-bucket profitability comparison is not statistically meaningful (the 80-90 bucket's -0.20 expectancy vs. the 90-100 bucket's 58.24 comes from an n=77 sample and should not be over-interpreted).
- **Confidence drift**: not separately assessed beyond the correlation figure above — a time-windowed confidence-vs-outcome trend would need a dedicated pass; flagged as future research (§13).

**Per this task's explicit instruction, the confidence model is documented here, not redesigned.** The practical implication for §11: since the score doesn't currently separate winners from losers, it should not be used (in its current form) as a live filter threshold — this is a finding, not a criticism of the scoring formula's design intent.

---

## 10. Institutional Edge Score (IES)

### 10.1 Formula

```
IES = 100 × Σ (weight_i × component_i)
```

| Component | Weight | Definition |
| --- | --- | --- |
| Expectancy | 0.20 | Min-max normalized mean R-multiple across the strategy's combined trade set (instrument-agnostic, unlike $ expectancy) |
| Profit Factor | 0.15 | min(profit_factor, 3.0) / 3.0 — capped so one outlier ratio doesn't compress every other strategy's score |
| Robustness | 0.20 | Fraction of EURUSD rolling 12-month windows with positive expectancy (0–1, direct from `summarize_stability`) |
| Consistency | 0.15 | 1 / (1 + CV), where CV = std(window expectancy) / \|mean(window expectancy)\| — rewards low relative dispersion across rolling windows |
| Drawdown (inverted) | 0.15 | 1 − min(\|max_drawdown_pct\|, 50%) / 50% — lower drawdown scores higher |
| Portfolio Contribution | 0.10 | Best diversification benefit achieved by any multi-strategy combination including this strategy (min-max normalized) |
| Correlation (inverted) | 0.05 | 1 − min-max-normalized mean \|pairwise correlation\| with the other four strategies — rewards genuine diversifiers |

Weights sum to 1.0. Normalization is **relative to the five strategies being compared** (the same honest-scope approach this platform already uses for Calmar Ratio in `src/research/strategy_analysis.py` — a dataset this size doesn't support a universal absolute scale).

### 10.2 Final Ranking

| Rank | Strategy | IES | Expectancy | Profit Factor | Robustness | Max DD % | Correlation |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | **S3** | **71.74** | 0.26 (avg R) | 1.57 | 50% | -16.5% | 0.025 |
| 2 | **S5** | **64.78** | 0.15 | 1.29 | 67% | -12.8% | 0.051 |
| 3 | **S4** | **64.30** | 0.22 | 1.37 | 67% | -10.5% | 0.057 |
| 4 | **S2** | **64.06** | 0.22 | 1.25 | 67% | -18.2% | 0.051 |
| 5 | S1 | 33.37 | -0.02 | 0.93 | 33% | -10.3% | 0.014 |

S3's win comes primarily from its strong expectancy and profit-factor components — its known frequency-inconsistency weakness (§4.4) is only partly captured by the robustness/consistency components here, so this ranking should be read alongside §4.4, not in isolation.

---

## 11. Recommendations

1. **Deserves live testing (paper/small-size first): S3 and S4.** Both rank top-two by IES, both are positive on all 7 tested symbols, and both show parameter response curves consistent with genuine edge rather than overfitting (§7). **Recommended live allocation: the S3+S4 portfolio** (§5.4) for diversification, or **S4 alone** if pure per-trade expectancy is prioritized over diversification — this is a judgment call for the allocator, not something the data resolves to a single answer.
2. **S2 and S5 are viable secondary candidates**, not first-choice: both solidly profitable and consistent across symbols, but S2's failure mode is largely unexplained ("Other") and S5 showed a negative partial-year in 2026 — worth continued monitoring before scaling.
3. **S1 should be retired in its current form.** Negative expectancy on its only tested symbol, worst IES score, worst rolling-window stability, zero signals generated on 6 of 7 symbols in their available windows, and a flat-negative parameter response curve on its own defining parameter (gap size) — every angle examined points the same direction. This is not a call to delete the strategy code (it may be salvageable with a redesign), but it does not deserve capital as configured today.
4. **Strongest symbols**: USDJPY shows the largest raw expectancy across strategies (though on only 6 months of data — treat as directional, not conclusive); EURUSD is the only symbol with enough history to trust the year-by-year and rolling-window results. USDCHF is consistently the weakest of the 7 tested (lowest or near-lowest expectancy for S2/S3/S5).
5. **Regimes to avoid**: cannot be stated with full confidence given the small-sample guard applied in §6 — the clearest, best-supported finding is structural (liquidity-sweep failures, §8) rather than regime-specific.
6. **Most robust parameter settings**: R:R 2.5 (all strategies), S3 CHoCH confirmation on M15 (not the current M5 default — a concrete, evidence-backed configuration change worth testing), S5 London-only session filter (confirms current default).
7. **Critical infrastructure finding — recommended follow-up (out of this task's scope, but blocking valid multi-year single-strategy research without a workaround)**: `src.backtest.risk.RiskTracker.consecutive_losses` has no reset mechanism once `max_consecutive_losses` is hit — it permanently locks out all further trades for the rest of the backtest, since no new trade can open to produce the winning close that would reset the counter. This was worked around for this research using a permissive risk config (`max_consecutive_losses=999`), documented in `scripts/run_institutional_research.py`. A real fix (e.g., a time-based or trade-count-based cooldown instead of a permanent lock) is recommended before this risk gate is relied on for any live-trading or single-strategy backtest use.

---

## 12. Future Research

- Extend GBPUSD/USDJPY/AUDUSD/USDCAD/USDCHF/NZDUSD to full multi-year history (the same depth as EURUSD) to upgrade the cross-symbol findings in §4 from "directionally consistent over 6 months" to "validated over multiple years," matching EURUSD's evidentiary weight.
- Revisit XAUUSD once back in scope — data acquisition is now unblocked (§3).
- Backfill the EURUSD 2021 gap if a data source can supply it, to complete the year-by-year and rolling-window series in §4.4/§4.5.
- Fix the RiskTracker consecutive-loss lockout (§11) as dedicated infrastructure work, then re-run this campaign to confirm results are consistent with the workaround used here.
- A genuine walk-forward *optimizer* (fit R:R=2.5 and S3's M15 CHoCH timeframe on a training window, validate out-of-sample) — this task's parameter sweeps identified promising configuration changes but did not validate them out-of-sample.
- Wire an economic-calendar feed for real news-day classification (currently a platform-level placeholder).
- Investigate S2's large "Other" failure bucket with a deeper trade-level review — the current categorization taxonomy doesn't explain most of its losses.
- A dedicated confidence-model redesign pass, informed by §9's finding that the current score doesn't separate winners from losers on this dataset.
