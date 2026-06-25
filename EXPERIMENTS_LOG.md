# Experiments Log — information-diffusion-lag strategy

A running record of every experiment, in order. For each: what we did, why, the data, what
changed from the prior step, the result/score, why it fell short, and the conclusion it forced.
Metrics use OOS (test) wherever a split exists; Sortino was not computed in most runs (noted).

Thesis under test: a Polymarket probability crossing ~55% flags a cross-sectional US-equity
mispricing during the information-diffusion lag; trade the exposed names over that window.

---

## Phase 0 — Raw theory tests

### E0.1 Naive long-only on 55% crossings
- **What:** when a market's probability crosses 55%, go long the LLM-mapped exposed equities, hold to resolution.
- **Why:** first direct test of the diffusion-lag thesis.
- **Data:** Polymarket hourly probabilities + daily equity bars + LLM asset worlds.
- **Result:** positive raw return, but the book was **~89% sector beta**.
- **Why it fell short:** the P&L was mostly market exposure, not the claimed mispricing.
- **Learned:** raw return is the wrong target — the real target is the **sector-hedged (idiosyncratic) return**. Beta must be removed before any claim of edge.

### E0.2 Diffusion-lag measurement
- **What:** quantify how much of the total move happens *after* the 55% crossing.
- **Why:** confirm the lag is real and there is post-signal move left to capture.
- **Result:** **≈5–9% of the move is post-crossing.** The lag is real but the catchable slice is small.
- **Learned:** the lag exists, but it is not the binding constraint — **direction is.** Getting the side right matters far more than the lag.

### E0.3 First full backtest
- **What:** end-to-end pipeline backtest (git: "first backtest failed and took way too long").
- **Why it failed:** too slow and crashed before producing results.
- **Learned:** the pipeline needed restructuring; performance and resumability had to be fixed before any signal work.

### E0.4 Second backtest, sentiment removed
- **What:** re-ran the pipeline with the news/sentiment layer cut (git: "without sentiment").
- **Why:** sentiment was not reproducibly backtestable and was not earning its place.
- **Learned:** **sentiment is excluded** from the tradeable pipeline.

---

## Phase 1 — Learned direction (price-trend ML)

### E1.1 Price-trend ML direction classifier
- **What:** ML classifier predicting trade direction from price-trend features.
- **Why:** if direction is the constraint, try to learn it.
- **Data:** per-trade price/momentum features around the crossing.
- **Result:** **0.39–0.46 OOS accuracy — anti-predictive** (worse than a coin flip).
- **Bug found & fixed:** a predictions join multiplied rows ~2.3×, producing a fake ~0.91 accuracy; corrected, the edge vanished.
- **Why it failed:** price-trend features carry no forward direction signal here.
- **Learned:** do not trust in-sample direction accuracy; **the LLM layer is misused, not misclassified**; the **momentum (run-up) filter is a genuine feature** even though the trend classifier is not.

---

## Phase 2 — Structural (non-learned) direction pockets

### E2.1 Long oil on supply-threat
- **What:** long crude/energy on geopolitical supply-shock events (Gulf/OPEC/Hormuz).
- **Why:** some directions are mechanical, not statistical.
- **Result:** positive idiosyncratic return — a real **structural** direction (not learned).
- **Learned:** **oil-on-supply-threat is a genuine catchable edge.**

### E2.2 Rate-cut leverage spread
- **What:** high-debt vs cash-rich cross-section on rate-cut events.
- **Result:** **high-debt +3.8% vs cash-rich −0.3% (10d)** — a real spread.
- **Why it stalls in-pipeline:** almost no real Fed crossings exist in the data, and they were mapped to bonds/Europe, not the leverage cross-section.
- **Learned:** rate-leverage is real but barely present in the dataset; needs fundamentals (debt) and better macro world-mapping to test.

---

## Phase 3 — Two-model architecture

Model 1 = alpha (per-trade long/short edge on the hedged return). Model 2 = portfolio layer
(per-iteration buy/sell/hold, constant size, optimize Sharpe / −maxDD). Split is chronological:
train ≤ 2025-12-31, val 2026-01-10…03-31, test ≥ 2026-04-10, with a 10-trading-day embargo.

### E3.1 Model 1 — XGBoost (Huber) on `y_hedged`
- **What:** regularized XGBoost regressor on the 10-day sector-hedged return; 23 features (oracle/world/price/fundamentals/cross-sectional/old-ML).
- **Why:** establish whether there is *any* per-trade edge before optimizing a portfolio.
- **Data:** `candidates.parquet`, 1,733 rows; median |y_hedged| ≈ 2.83%.
- **Result:** **train rank-corr 0.84 → test −0.05 (FAIL)**; overfit gap 0.885; top-quintile test +0.73%. Linear baseline: val −0.28, test −0.0015. Ablating any feature group barely moved OOS.
- **Why it failed:** massive overfit; no out-of-sample rank-correlation; direction not recoverable from these features.
- **Learned:** there is **no learned per-trade edge** in this feature set; in-sample numbers are noise until OOS confirms.

### E3.2 Model 2 v1 — rule-based portfolio sweeps
- **What:** entry-band (strong≥{0.60,0.65,0.70}, 2-day confirm) × exit (Polymarket reversal θ_out∈{0.50,0.55}, stop type {fixed%, fixed-trail, close-vol-trail}) × objective {Sharpe, Calmar, return-s.t.-maxDD} × universe {all, pre_drop}. Tuned on train, picked on val, reported on test. Each leg sector-hedged, constant $10k on a $100k book.
- **Why:** a robust rule floor before any learned policy.
- **Result (OOS, "all", Sharpe objective):** train Sharpe **3.747 → test 0.127**; maxDD 4.77%; net +0.11%; hit 43%; turnover 46.8. Best stop = close-vol trailing. **OOS baselines beat it:** long-only 2.10, current-ML 2.41, random-alpha 2.12, equal-weight −0.23. `pre_drop` showed test Sharpe 4.5 but on **n=3 trades** (noise). (Sortino not computed.)
- **Why it failed:** the rules overfit train; OOS Sharpe collapsed and did not beat naive long-only / random on the full universe.
- **Learned:** with no Model-1 edge to stand on, the portfolio layer has nothing to time/select; v1 is a floor, not an edge. B4 (RL) stays gated until a real per-trade edge exists.

---

## Phase 4 — Fixing the LLM world layer

### E4.1 Diagnose the deployed worlds
- **What:** read the actual worlds in the DB feeding the dataset.
- **Found:** stale **one-pass "compact" peer-dumps** — earnings → 7–8 peers, padded to ≥4 names, `connection_strength` NULL on all 10,329 picks, ~16–21% dead worlds, spurious defense mappings.
- **Correction (important):** the earnings peer-dump **never reached training** — `ml_observations` already collapses earnings worlds (7.8 assets) to ~1 valid observation. The real multi-name noise in the 1,733 candidates is the **military rows** (defense ~4.3/mkt, energy ~2.1/mkt).
- **Learned:** judge the worlds on **relatedness to the cause**, not on whether names moved (the model learns movement); `connection_strength` from the LLM is self-graded and worthless unless it tracks real relatedness.

### E4.2 Two-pass world builder (relevance gate → tight mapping)
- **What:** pass 1 relevance gate; pass 2 tight mapping (earnings→1 name, no peers; geopolitical→oil only; macro→rate-sensitive; each with `connection_strength`).
- **Changed from E4.1:** replaced the compact peer-dump path; added a relevance score; cache-busted (`prompt_version` v7) so a re-run actually rebuilds.
- **Bugs found & fixed:** Gemini rejects the nested server JSON schema (HTTP 400) → prompt-schema mode; the gate over-dropped *all* geopolitical/macro → recalibrated to keep oil/rate edges; an untradeable named company (APLS) hard-crashed the whole run → emit an empty world instead.
- **Live check:** earnings→1 name (cs 1.0); Israel→Iran→USO/ITA; inflation→TLT; Fed cut→TLT/QQQ/ITB/XLF; defense correctly dropped from strike events.
- **Learned:** the worlds are now *related*, but relatedness alone does not create edge — see E4.3–E4.4.

### E4.3 Model performance under the new worlds (pre-rebuild proxy)
- **What:** retrain Model 1 on the existing dataset filtered to the new-world candidate sets (drop military-defense; earnings+fda only ≈ relevance≥0.5; drop all military; earnings only).
- **Result:** **every variant FAILs OOS** (test rank-corr −0.003 … −0.069; best top-quintile +1.30% for earnings+fda). **Test is 97% earnings (212/218)**; earnings candidates are unchanged by the rebuild.
- **Why it failed:** the rebuild changes military/macro worlds, which are *not* in the earnings-dominated test window; earnings direction is a coin flip (test share-up 42%).
- **Learned:** the world rebuild **cannot** flip the alpha model — its value is the relevance filter + clean worlds for the **portfolio** side, not earnings-direction prediction.

### E4.4 Oracle ceiling on the relevance≥0.50 names
- **What:** strip the model and the hedge. For the names clearing 0.50, enter at the 55% crossing and take the perfect direction + perfect exit (raw move).
- **Result:** **oracle median 6.3% / mean 9.5%** (range 2.7–26.7%). But realized direction is a coin flip (ABT −2.7%, TLT −6.3% went the *wrong* way), and the highest-relevance market (**Fed emergency cut never crossed 55%** → no trade). n=5 (thin).
- **Learned:** the raw opportunity is real (~6%), but **direction is the entire game**, and **crossing 55% is a separate filter** from relevance (high relevance ≠ a trade). This is why the next test is the full-sample naive oracle, not another model.

### E4.5 Question-relevance scoring (× stock connection)
- **What:** pass 1 now rates the **question's** real-world US-equity impact ∈ [0,1] (anchored: earnings/Fed ~1.0, US-direct/oil-shock ~0.8, ally-conflict ~0.5, routine macro ~0.3, Houthi ~0.2, speech-word ~0). Final relevance = `question_relevance × connection_strength`; trade ≥ 0.50; nothing hard-dropped except question_relevance < 0.10.
- **Changed from E4.2:** the gate became a graded score instead of a binary drop (preserve thin data); macro now includes **QQQ / long-duration & high-debt names (Tesla-type)** because cheaper money lifts heavy borrowers and dearer money sinks them.
- **Live preview:** earnings 1.00; Fed cut 1.00 (TLT/QQQ/ITB/XLF all ≥0.5); inflation 0.60 (TLT 0.54 survives, rest filtered); Israel→Iran 0.50 → USO 0.40 (filtered); Houthi 0.20 (dropped). Inflation later nudged toward 0.3.
- **Learned:** score relevance, don't waste data; many *questions* (not just stocks) have no US-equity channel; the 0.50 cut keeps earnings + Fed + US-direct and filters ally-conflict + soft-macro.

### E4.6 Full rebuild on the two-pass worlds (run `5fb1a1cd`, complete)
- **What:** full pipeline rebuilt with the two-pass worlds + relevance score. 936 candidates (vs 1,733 — the military peer-dumps are gone; earnings unchanged).
- **Mid-run fixes:** untradeable named companies (APLS, BK, ZI, EB, VSCO, ...) → emit an empty world instead of crashing the run; a Gemini **monthly spend-cap** stall → resumed from the 940 cached worlds.
- **Result — Model 1 (RF, direction+magnitude) on clean worlds:** train rank-corr **0.913 → test −0.075 (FAIL)**, overfit gap 0.99. RF direction hit-rate OOS: val 43%, **test 48–50% (coin flip)**.
- **Why it failed:** clean worlds don't create a direction signal; the test window is ~all earnings, whose direction is a coin flip.
- **Learned:** world quality was real but it is **not** the alpha bottleneck — direction is, and it's unpredictable OOS.

### E4.7 Naive oracle + exit-rule ablation A–E (run `5fb1a1cd`)
- **What:** relevance ≥ 0.5 universe. Oracle ceiling vs naive long-the-beneficiary; then exit rules A–E each tested **separately**, under RF-direction vs long-the-beneficiary.
- **Naive oracle (test):** oracle median 7.3%; naive long +2.03% at 51% hit. By archetype: earnings **flat (+0.21%, 49%)**, oil-on-military **+6.97% (55%)**, FDA **+10.6% (n=14)**, macro/"unknown" **+9.2% (75%)**.
- **RF-direction vs long (same exits, base, test):** RF picks direction → Sharpe **−5.05, net −7.75%, hit 38%**; long-the-beneficiary → Sharpe **+4.38, net +6.95%, hit 46%**. ~14 pp OOS swing.
- **Exit rules (long book, test net vs base +6.95%):** A take-profit fixed@0.08 **+8.97%**; A **RF-magnitude +9.49% (hit 60%, maxDD 3.09%) — best**; B ratchet, C pre-event, D stall all **hurt**; E prob-ceiling ~neutral, lower maxDD. (Sortino not computed.)
- **Why it's not a locked edge:** base is **test-window-favorable** (train Sharpe 0.15 / val 0.33 / test 4.38); the A variants that win on test are **negative on val** → not robust across splits.
- **Learned:** the RF must **not** pick direction (it's harmful, −14 pp); RF *magnitude* is mildly useful for the take-profit; take-profit is the only OOS-positive exit rule; go long the beneficiary.

### E4.8 Distribution-pooling / overfitting caveat
- **Insight:** the candidates are **not one distribution** — earnings (flat, ~3% moves, binary announcement gap), FDA (~28% moves), oil-on-military (structural up-bias), macro (~75% up) are different regimes. **Train holds oil/macro/FDA; test is ~all earnings** → training one RF across all archetypes and fitting one take-profit level pools mismatched distributions. The +9.49% RF-magnitude and the fixed 8% are therefore **mostly luck on this particular mix**, not a captured per-trade distribution.
- **Implication:** pooling across archetypes is invalid; model + exit should be **per-distribution (per-archetype)**, or each structural pocket treated on its own terms. Not decisive, but we are overfitting.
- **RL (Model 2 learned policy) NOT run** — and premature: a learned policy on a pooled, distribution-mismatched ~400-row train set would overfit hardest. Separate distributions before any RL.

### E4.9 Per-archetype study + fundamental clustering (run `5fb1a1cd`)
- **What:** stop pooling. Each archetype gets its own split (earnings = real chronological; FDA = 7/2/5; oil-military = 70/15/15), its own RF (fundamentals for earnings/FDA; +Polymarket score for oil), and a fundamental cluster breakdown.
- **Earnings (n=804):** the pooled RF still fails (test dir_hit 40%). But the **cluster signal is real and lives in SECTOR**: Technology beats +2.33% / 61% up, Consumer Defensive +0.96% / 59%, Industrials 54% — vs Consumer Cyclical −1.71% / 37%, Comm Services 42%, Financials 43%. Mild support for mega-cap (52% vs 48%) and low-debt (52% vs 45%). The signal is a **rule (long Tech/Defensive beats), not an RF prediction** — and the sector cut is pooled, so it still needs OOS validation.
- **FDA (n=14):** lottery — train +7.45% (86% up) → test −1.27% (20%). Un-modelable.
- **Oil-military (n=118):** train +0.88%, **val +14.98% (83%)**, **test −2.02% (29%)** — regime-dependent (huge only when supply is actually threatened); not a stable rule.
- **Learned:** every archetype that wins in one split loses in another — the per-archetype splits **empirically confirm the overfitting/distribution caveat**. The one modelable lead is earnings-by-sector; the structural pockets are real only in specific windows.

### E4.10 RL 'feel' — CEM over exit knobs (run `5fb1a1cd`)
- **What:** a minimal learned Model-2 policy (cross-entropy-method search over entry band, theta_out, vol-stop k, take-profit) maximizing train Sharpe; long-the-beneficiary direction. Not a full neural RL.
- **Result:** train Sharpe could not exceed ~0.9 (final policy 0.29); val 0.20; **test 4.24 (net +7.83%, maxDD 2.10%)** — but that equals the plain long base (4.38), i.e. the favorable-earnings-window, not learned edge. It converged to `strong>=0.65 + close_vol_trailing@3 + poly<0.52 + take-profit`.
- **Learned:** there is nothing to learn yet — a flexible policy rediscovers "long the beneficiary" and inherits the same window-dependent OOS. A full RL would only overfit harder. Gate it until a structural pocket has real, validated OOS.

---

## Standing conclusions carried forward
- Target = sector-hedged (idiosyncratic) return; raw return is ~89% beta.
- Direction is the binding constraint; the lag is real but small (5–9% post-crossing).
- Price-trend ML direction is anti-predictive (0.39–0.46 OOS); momentum run-up filter is genuine.
- Sentiment excluded. Structural edges (oil-on-supply, rate-leverage) are real but thin in-data.
- No learned per-trade edge yet (Model 1 OOS FAIL); Model-2 v1 does not beat naive baselines OOS.
- Worlds must be *related to the cause*; relevance is now a graded score, traded above 0.50.
- **The RF must not pick direction — it is harmful (−14 pp OOS vs long-the-beneficiary).** Its job is
  selection + magnitude (the magnitude usefully sizes the take-profit; the direction is a coin flip).
- **Go long the structural beneficiary; harvest with a take-profit** (the only OOS-positive exit rule).
- **Do not pool archetypes** — earnings / FDA / oil-on-military / macro are different distributions, and
  train vs test are different archetype mixes. Pooled fits (one RF, one take-profit level) overfit a
  blend. Model + exit must be per-distribution. RL is premature until distributions are separated.
