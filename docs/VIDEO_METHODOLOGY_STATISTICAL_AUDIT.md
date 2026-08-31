# Statistical Audit of the Video's Methodology (Task 11.5 Phase 5)

This audit evaluates the METHODOLOGY described in `docs/VIDEO_25K_ICT_RESEARCH_METHODOLOGY.md`, not a specific strategy — because the reconstruction does not give us the actual winning strategy's rules in enough detail to audit a specific backtest. The central question: **does "25,000 configurations tested, ~1.7% survived" constitute evidence of a real edge?** The honest answer is: not on its own — it depends entirely on details the reconstruction does not specify.

## 1. The multiple-testing problem, quantified

If you test **N = 25,000 independent strategy configurations that all have TRUE expectancy exactly zero** (pure noise, no real edge, after costs), and you flag a configuration as "looks profitable" whenever its backtest performance crosses a threshold that corresponds to a one-sided **p < 0.05** significance level (a common, if weak, bar), then **by the definition of a p-value**, approximately 5% of those zero-edge configurations will cross that threshold by chance alone:

```
Expected false positives (uncorrected, α = 0.05) = 0.05 x 25,000 = 1,250 strategies
```

That is the number of strategies that would "look like they work" purely from randomness, with zero real edge, using a fairly permissive per-test bar. The reconstruction reports **~1.7% survived** (≈ 425 of 25,000) a stricter, multi-criteria filter (profitability + robustness). 425 is LESS than the naive 1,250-false-positive estimate — which is mildly reassuring (it suggests the filter is doing more than "was profitable once"), but this comparison is only illustrative: we do not know whether the actual filter's effective significance threshold was looser or tighter than p < 0.05, so we cannot conclude how many of the 425 survivors are real.

**Family-wise error correction (Bonferroni).** To hold the probability of ANY false positive across all 25,000 tests at 5%, the correct per-test threshold is:

```
α_corrected = 0.05 / 25,000 = 0.000002  (roughly a 4.6-sigma bar)
```

This is a genuinely severe bar, and pure Bonferroni is known to be overly conservative here because the 25,000 configurations are **not independent** — many share overlapping parameters (e.g., a strategy with FVG-min-size=0.10% and one with 0.11% produce highly correlated trade sequences). The correct tool for this exact problem (many correlated backtests, pick the best, ask "is that best one real?") is the **Deflated Sharpe Ratio / Probability of Backtest Overfitting** framework (Bailey, Borwein, Salehipour & López de Prado, 2014 — established quantitative-finance literature, cited here as general methodology, not sourced from the video). **SozoFXSMC does not currently implement this** — flagged as a genuine gap (see Phase 6 experiment plan and Phase 4's Category C in the comparison doc).

## 2. Survivorship bias

The reconstruction tests strategies on NQ and ES only — both currently-listed, liquid, continuously-existing instruments. This is a mild but real form of survivorship bias: the research implicitly conditions on "instruments that still exist and are still liquid 16 years later," which is a much weaker bias than, e.g., testing only stocks that didn't go bankrupt, but is worth naming. For our Forex transfer: EURUSD/GBPUSD/etc. carry essentially the same property (currency pairs that have existed and remained liquid for the full backtest window) — not a new risk introduced by adopting this methodology, but not eliminated either.

## 3. Look-ahead bias

**UNKNOWN whether addressed at all** — the reconstruction never mentions signal timing discipline, entry-on-next-candle rules, or any explicit look-ahead-prevention mechanism. This is a real, unaddressed gap in what we know about the video's methodology. By contrast, SozoFXSMC's existing strategy code has this as a first-class, tested invariant (`tests/test_no_lookahead.py`, and every `MarketContext` accessor is explicitly asof-safe — see `src/strategies/context.py`). **If we adopt the video's parameterization approach, we must implement it inside our existing no-look-ahead architecture, not import an architecture whose look-ahead discipline is unverified.**

## 4. Parameter overfitting

This is the headline risk with a 25,000-configuration (sampled from 258M) search, and the reconstruction is explicit that the creator was aware of it (§15-16, §25 in the methodology doc). The prescribed defense — parameter STABILITY testing ("does X±1 still work?") — is the correct approach and is directly compatible with SozoFXSMC's existing `src/research/sensitivity.py::parameter_response_curve` / `detect_diminishing_returns`. Whether this defense was ACTUALLY applied to the reported winning strategy, versus only described as best practice, is UNKNOWN from the reconstruction.

## 5. In-sample vs. out-of-sample performance

The reconstruction lists out-of-sample testing and walk-forward testing among the RECOMMENDED steps (§16, §24), but does not report concrete in-sample-vs-out-of-sample numbers for the actual winning configuration (e.g., "62% expectancy in-sample, 58% out-of-sample"). Without that comparison, **we cannot verify the 1.7% survival rate reflects genuine out-of-sample robustness rather than in-sample curve-fitting that happened to also pass a loose robustness screen.** This is the single most important unresolved question about the video's methodology.

## 6. Strategy selection bias

Even if EVERY individual test in the 25,000-configuration search were methodologically sound, the act of **selecting the best-looking result out of thousands** is itself a bias-inducing procedure — this is exactly what the multiple-testing math in §1 quantifies. The reconstruction's own framing (§15, "The Overfitting Problem") shows explicit awareness of this, which is a point in the methodology's favor relative to naive backtesting content — but awareness of a bias is not the same as a rigorous correction for it (see §1's Deflated Sharpe Ratio point).

## 7. Transaction-cost assumptions

Costs (commission + slippage) are stated to be included, which is good practice and better than many public backtests. **Exact magnitudes are UNKNOWN** — for NQ/ES futures, typical costs (per-contract commission, 1-2 tick slippage) are very different in character from Forex costs (spread-dominated, often 1-2 pips on majors, wider on exotics, plus swap for overnight holds). This is precisely why the reconstruction itself (§21) says NOT to copy the NQ/ES parameters directly to Forex — a conclusion this audit agrees with independently.

## 8. Regime dependence

**UNKNOWN** — the reconstruction does not report whether the winning strategy's performance was tested/stable across distinct market regimes (trending vs. ranging, high vs. low volatility) within the 16-year NQ/ES window, only that "different market periods" is listed among the recommended robustness checks. SozoFXSMC already has regime-labeling infrastructure (`src/research/market_conditions.py`, used throughout Tasks 8-10's institutional research) that the video's approach, as described, does not appear to have an equivalent of.

## 9. Symbol dependence

Tested on exactly 2 correlated instruments (NQ, ES — both US equity index futures, historically highly correlated with each other). This is a narrower symbol-diversity test than it might first appear: NQ and ES moving together for macro reasons doesn't independently validate a "liquidity sweep" edge across genuinely different market structures. SozoFXSMC's own Task 8 research already spans EURUSD (full history) plus 6 other pairs (6-month depth) — a broader, if still limited, symbol base. Transferring the video's methodology to Forex is itself a symbol-dependence test the video never performed.

## 10. Session dependence

The reconstruction's own finding — that the specific ICT Silver Bullet kill-zone windows did NOT show extraordinary edge over other tested windows — is itself a session-dependence finding, and a valuable one: it argues against assuming any particular session/time window has special properties without testing. This is directly re-testable against SozoFXSMC's own S3/S4 data via the existing `src/research/session_analysis.py`.

## Bottom line

**The fact that a strategy was selected from 25,000 tested configurations does NOT, by itself, mean it is robust.** Quantitatively: an uncorrected p<0.05 bar applied to 25,000 zero-edge configurations would be expected to produce ~1,250 that "look profitable" by chance alone; a properly Bonferroni-corrected bar would require roughly a 4.6-sigma result, and even that is likely too conservative given the tests are correlated, not independent (the correct tool is a deflated/probabilistic Sharpe ratio approach, which neither the video's described methodology nor SozoFXSMC currently implements). The reconstruction shows genuine awareness of overfitting risk and prescribes reasonable defenses (out-of-sample testing, parameter stability regions, Monte Carlo) — but does not give us enough concrete detail (exact thresholds, actual in-sample vs. out-of-sample numbers) to confirm those defenses were rigorously applied to the specific ~425 surviving configurations. **The methodology is directionally sound; the reported result (1.7% survival) is neither validated nor invalidated by what we know.**
