# Sharpe-optimizing portfolio model — build & validation plan

Goal: one learned model that (a) **filters** out the events/assets we can't profit from and
(b) **allocates** the $100k portfolio to maximize a chosen objective (default **Sharpe**;
swappable to Calmar / return-subject-to-max-drawdown). It must keep the LLM's unbiased
generalization to novel events — we learn *what to drop and how to size*, we do **not** hard-code
direction rules.

Dataset is small and lopsided (**1,733** valid candidates, 2024-Q3→2026-Q2, ~78% in the last 3
quarters). **Overfitting is the primary risk.** Out-of-sample is the only score that counts.

Open decisions for Liran are marked **[DECIDE]**. Don't proceed past one without his call.

---

## Stage 0 — Data foundation (no model; must be exact)

**0a. Ingest fundamentals** (we have none today — `historical_asset_metadata.metadata` only holds
sector/exchange). For every traded symbol, pull from yfinance `.info` and store in a new table
`historical_asset_fundamentals(symbol, as_of, debt_to_equity, total_debt, total_cash, market_cap,
beta, profit_margin, free_cash_flow, updated_at)`. These power the leverage/rate-sensitivity
features. *Caveat to log: yfinance `.info` is point-in-time-now, not as-of-event — acceptable for a
first pass, but note the mild look-ahead.*

**0b. Build the labeled candidate table** — one row per `(run_id, market_id, symbol, T_θ)`:
- Source rows: `historical_ml_observations` (valid_for_training) joined to `historical_run_markets`
  (question, created_at = T0, final_outcome) and the rebuilt worlds (Stage 1).
- **Label** `y_hedged` = `asset_ret(entry→exit) − sector_etf_ret(entry→exit)`, where
  `entry` = first daily close ≥ T_θ, `exit` = first close ≥ `min(T_e − 1 trading day, entry + max_holding_days)`,
  `max_holding_days = 10` **[DECIDE: horizon]**. Sector ETF from `asset_metadata.sector_etf`
  (fallback SPY). This is the sector-neutral idiosyncratic return — the thing we proved is the real
  target (raw return is mostly beta).
- Also store `y_dir = sign(y_hedged)` and `realized_|move|` for diagnostics.

**0c. Feature matrix X** (all strictly ≤ T_θ — no look-ahead):
- *Oracle:* `prob_at_trigger, prob_slope_24h, prob_volatility, prob_surge_since_T0, time_to_resolution_days, crossing_latency_days, pre_entry_volume_log` (from `historical_probability_points` + `features`).
- *World / LLM:* `connection_strength` (Stage 1), `archetype` (categorical), `world_size`, `asset_role`.
- *Price:* `runup_since_T0 (T0→T_θ), asset_2w_trend, sector_1m_trend, spy_2w_trend, ytd_change`.
- *Fundamentals (0a):* `debt_to_equity, cash_to_marketcap, beta, profit_margin, log_market_cap, sector`.
- *Cross-sectional within world:* `runup_rank, size_rank`.
- *Old-model outputs as FEATURES (DECIDE G — Liran's instinct: yes):* the legacy ML
  `classification_probability, predicted_peak_percent, direction, directions_agree`, and the
  momentum ROC. We are **not** going back to these as deciders — the RF is the brain; these are just
  inputs it may learn to trust or ignore. Ablate whether they add OOS value.
- *(Option B only) portfolio state:* `gross_exposure, net_exposure, sector_exposure, open_positions, running_pnl, drawdown` — recomputed during the simulated walk.

Deliverable: a single reproducible script `general_testing/build_dataset.py` that writes
`candidates.parquet`. **Validation:** row count == valid-observation count after Stage-1 rebuild;
no NaNs in required features; label distribution sane (median |y_hedged| ≈ 3–6%).

---

## Stage 1 — Rebuild the worlds (better LLM, still unbiased)

Keep the LLM (it must generalize to novel questions), but make it tighter via a **two-pass prompt**:
1. **Relevance gate:** "Does any liquid US equity/ETF *mechanically* reprice if this resolves YES?
   If not, return empty." → drops the ~21% dead worlds and irrelevant questions.
2. **Tight mapping + confidence:** for single-entity events (earnings/FDA/M&A) return **only the named
   entity**; for macro/geopolitical return the specifically-exposed names (not the whole sector) each
   with a **`connection_strength` ∈ [0,1]** = how mechanically the outcome drives that asset's value.

Store `connection_strength` per asset; regenerate worlds for all markets.
**Validation (objective):** re-run `world_quality_audit.py` → fewer dead worlds, earnings worlds = 1
name, and `connection_strength` is positively correlated with realized `|y_hedged|`. If it isn't,
the score is noise — fix the prompt before training on it.

---

## Stage 2 — The model (filter + decide)

**You cannot optimize Sharpe on a signal with no edge.** So do 2a (is there per-trade edge?) before
2b (allocate for Sharpe). Both are below; **[DECIDE B: do A-then-B AND end-to-end B — Liran: test all]**.

### TWO models (Liran's architecture)
- **Model 1 — RF (alpha signal).** Per asset, at the crossing, outputs **long / short** (+ strength).
  Answers "is there an edge, which way." This is Stage 2a.
- **Model 2 — Portfolio Layer (the decision-maker).** Runs **every iteration**; for **each asset
  held or just-suggested by the RF**, picks **{buy, sell, hold}** from: the RF recommendation + the
  **live Polymarket probability** (e.g. sell when it crosses back below the band) + position/portfolio
  state. **Allocation per position is CONSTANT** — it optimizes *which positions and when*, not size.
  **Objective: maximize Sharpe OR minimize max-drawdown** (config; tested one-by-one, decision A).
  This layer **owns entry and exit dynamically** — exit is no longer a fixed rule; the time cap `F`
  is only a safety floor.

**Interface [DECIDE I]:** RF scores **once at the crossing** (lean — clean separation) or **re-scores
each iteration** (heavier loop).

**Build order for Model 2 (Liran: test all → this is the order):**
1. **Rule-based policy** (≈3 thresholds: enter on RF-long & prob>θ_in; exit on prob<θ_out OR RF-flip
   OR time-cap `F`; else hold), tuned for the objective on train, validated OOS. Robust floor.
2. **Learned policy** (RL / differentiable per-step hold-prob) generalizing the rules. Frontier;
   overfits hard on 1,733 candidates — guilty until OOS. Constant size + 3-action space keep it
   tractable.

### 2a — per-trade edge (foundation, low overfit risk) — *recommended first*
- Model: **XGBoost regressor** predicting `y_hedged`. Regularized: `max_depth=3–4,
  min_child_weight≥5, subsample=0.7, colsample_bytree=0.7, n_estimators` via early-stopping on val.
- **Loss: Huber** (robust to the ±40% earnings outliers). Output `ŷ_i` = expected hedged return.
- Baselines it must beat OOS: predict-zero, equal-weight, current ML book, long-oil-on-escalation.
- **Success criterion:** OOS rank-correlation(`ŷ`, `y_hedged`) > 0 *and* top-quintile-by-`ŷ` has
  positive mean hedged return. If not, there is no per-trade edge and Stage 2b is moot — stop and
  go back to Stage 1 / features.

### 2b — the Portfolio Layer (Model 2): per-iteration {buy/sell/hold}
Constant size → this is a **timing + selection** policy, not a sizing optimizer (see "TWO models").
Constraints throughout: per-sector cap, one position per name, each leg sector-hedged, $100k book.
- **v1 — rule-based (robust floor).** Enter on `RF=long & prob>θ_in`; exit on `prob<θ_out` OR RF-flip
  OR time-cap `F`; hold otherwise. Tune `{θ_in, θ_out, F}` on **train** for the objective
  (Sharpe / minimize maxDD); validate OOS. ~3 parameters → hard to overfit. Every learned version
  must beat this.
- **v2 — learned policy (frontier).** Per asset per step output `P(hold)`; **reward = Sharpe
  (or −maxDD)** of the resulting daily portfolio path; train via RL (policy gradient) or a
  differentiable path, with turnover/gross/sector-concentration penalties (these hold down maxDD).
  3-action space + constant size keep it tractable, but it overfits hard on 1,733 candidates —
  **guilty until OOS**, and only after v1 sets a floor and 2a shows real per-trade edge.

---

## Stage 3 — Chronological split & evaluation (DECIDED: chronological only)

Split by `T_θ` (the **real-world** probability-crossing time — verified: not a DB artifact) with a
**10-trading-day embargo** (= the holding window) between splits so a train trade's outcome can't
leak into val/test. This is exactly how live deployment runs: train on the real past, predict the
real future.

- **Train:** `T_θ ≤ 2025-12-31`  (≈829 rows)
- **Val:**   `2026-01-10 ≤ T_θ ≤ 2026-03-31`  (≈634 rows)
- **Test:**  `T_θ ≥ 2026-04-10`  (≈270 rows)

**Real-world timeline (verified, drives this split naturally):** Polymarket created its first
geopolitical market 2023-10-12 and its first *earnings* market only 2025-09-15. So geopolitical is
genuinely seen throughout; **earnings/FDA are genuinely new types that arrive late** — meaning the
chronological split already tests generalization to newly-introduced event types, for free, the way
reality introduced them. (Leave-one-archetype-out was considered and dropped per Liran: chronological
only.)

**Metrics (report all, OOS):** annualized Sharpe (primary), max drawdown, Calmar, net return %,
hit rate, turnover, and the **in-sample-vs-OOS Sharpe gap** (the overfit tell). Ablate each feature
group — if a group doesn't add OOS Sharpe, drop it.

---

## Guardrails (non-negotiable, given n=1,733)
Purged walk-forward + embargo; OOS-first; **linear/logistic baseline before trees before any NN**;
report IS vs OOS every time; assume in-sample gains are fake until OOS confirms.

## Known data gaps to close first
1. **No fundamentals** → Stage 0a (blocks the rate-sensitivity features).
2. **Almost no Fed crossings, mapped to bonds/Europe** → fix Stage-1 world-building for macro and
   confirm Fed questions are ingested, or the rate-sensitivity edge can't be tested in-pipeline.
3. **No `connection_strength`** → Stage 1.

## Decisions log (Liran)
- **Architecture:** **two models** — RF alpha signal (Model 1) + Portfolio Layer per-iteration
  {buy/sell/hold}, constant size, optimizing Sharpe/maxDD (Model 2). ✅
- **A — objective:** test Sharpe, Calmar, return-s.t.-maxDD **each one-by-one**. ✅
- **B — Model 2 build order:** v1 rule-based floor, then v2 learned (RL/differentiable). Test all. ✅
- **C — split:** **chronological only** (verified real-world). ✅
- **D — fundamentals:** pull what yfinance exposes; **caveat: `.info` is point-in-time-now**, so it
  may not match historical event dates (mild look-ahead) — try it, measure, flag. ✅
- **E — universe:** test **both** (all-archetypes-let-it-filter, and pre-dropped); Liran expects
  all-archetypes to win. ✅
- **F — time cap:** safety floor for Model 2's exit (default **10 trading days**, or 1-day-before
  resolution if sooner). Confirm 10 or change. ⬜
- **G — old ML + momentum as features:** **yes** (ablate their OOS value). ✅
- **H — exit policy:** owned by the Portfolio Layer (dynamic, Polymarket-cross-down + RF-flip +
  cap), **not** a fixed rule. Size constant. ✅
- **I — RF scoring cadence:** once-at-crossing (lean) vs re-score each iteration. ⬜

Open before Stage 0 starts: **F** (confirm 10-day cap) and **I** (RF cadence).
