# AGENT TASK — Track A: Data foundation + RF/XGBoost alpha model  (CODEX)

You are **Agent A**. A second agent (**Agent B / Claude Code**) builds the Portfolio Layer in
parallel on another branch. You two meet at **one contract** (the `candidates` dataset, below).
Build it; Agent B consumes it.

**Branch:** `model/alpha-dataset` (off `master`). **Repo:** `C:\Users\Liran\PycharmProjects\my_traders`,
venv `.venv\Scripts\python.exe`, Postgres schema `checking_relevant_events`
(`database/db_connection.py`). **Read first:** `WORKING_WITH_LIRAN.md`, `MODEL_PLAN.md`.

## Shared context (read, do not re-derive)
- Strategy: a Polymarket probability crossing 55% flags an equity mispricing during the
  information-diffusion lag; trade the exposed names over that window.
- **Proven (treat as given):** the lag is real (~5–9% of the move is *after* the crossing);
  **direction is the binding constraint**; the real target is the **sector-hedged (idiosyncratic)**
  return, not the raw return (raw is ~89% beta); the price-trend ML direction is anti-predictive
  (0.39–0.46 OOS); the **momentum (run-up) filter is a genuine feature**; sentiment is unbacktestable
  (excluded); structural-direction events (oil-on-supply, rate-cut leverage) are the catchable ones.
- **Two-model architecture.** *You build Model 1.* Model 1 = the alpha: per asset, at the crossing,
  predict **long/short edge** on the hedged return. Model 2 (Agent B) = the Portfolio Layer that
  decides buy/sell/hold each iteration. Models are different tools: yours is a **tree predictor
  (RF or XGBoost)**, Agent B's is **rules → RL**.

## ★ THE CONTRACT (must match Agent B exactly) ★
You produce `data/candidates.parquet` via a committed, reproducible script
`general_testing/build_dataset.py`. **One row per tradeable candidate.** Exact columns:

| column | type | source / definition |
|---|---|---|
| `run_id, event_id, market_id, symbol, pass_number` | keys | `historical_ml_observations` |
| `t0` | ts | market `created_at` (`historical_run_markets`) |
| `t_theta` | ts | `first_pass_at` (real-world 55% crossing) |
| `t_e` | ts | `label_available_at` (event end / resolution) |
| `entry_price` | float | first daily close ≥ `t_theta` (`historical_price_bars` 1d) |
| `sector_etf` | text | `historical_asset_metadata.sector_etf` (fallback `SPY`) |
| `y_hedged` | float | **label**: `asset_ret(entry→exit) − sector_etf_ret(entry→exit)`, exit = first close ≥ `min(t_e − 1 trading day, entry + 10 trading days)` |
| `realized_dir` | int | `sign(y_hedged)` |
| `realized_abs_move` | float | `max(\|max_change\|,\|min_change\|)` from `research_data` |
| `feat_*` (the X block below) | float/cat | see Tasks |
| `alpha_score` | float | **YOU fill via Model 1**: signed edge (sign=direction, magnitude=strength) |
| `alpha_dir` | text | `'long'` if `alpha_score≥0` else `'short'` |
| `split` | text | `train` if `t_theta ≤ 2025-12-31`; `val` if `≤ 2026-03-31`; else `test` (10-trading-day embargo between) |

Agent B reads this parquet **plus** the raw price series (`historical_price_bars`) and probability
series (`historical_probability_points`) from the DB for `[t_theta, t_e]`. **You do not own exits or
sizing.** Lock this schema with Agent B before writing the model.

## Your tasks
**A0 — fundamentals ingest.** New table `historical_asset_fundamentals(symbol, as_of,
debt_to_equity, total_debt, total_cash, market_cap, beta, profit_margin, free_cash_flow, updated_at)`
from yfinance `.info` for every traded symbol. **Caveat to log loudly:** `.info` is point-in-time-now,
not as-of-event — mild look-ahead; measure coverage and flag missing names.

**A1 — rebuild worlds (improves features; ship A2 on existing worlds first so B isn't blocked).**
Two-pass LLM: (1) relevance gate — "does any liquid US equity/ETF *mechanically* reprice if YES? else
empty"; (2) tight mapping — single named entity for earnings/FDA, specifically-exposed names for
macro/geopolitical, each with `connection_strength ∈ [0,1]`. Validate with `world_quality_audit.py`:
fewer dead worlds, earnings→1 name, `connection_strength` correlates with realized `|y_hedged|`.

**A2 — build `candidates.parquet`** (the contract). Feature block X:
- *Oracle:* `prob_at_trigger, prob_slope_24h, prob_volatility, prob_surge_since_t0,
  time_to_resolution_days, crossing_latency_days, pre_entry_volume_log` (from `features` +
  `historical_probability_points`).
- *World:* `connection_strength, archetype(cat), world_size, asset_role(cat)`.
- *Price:* `runup_since_t0, asset_2w_trend, sector_1m_trend, spy_2w_trend, ytd_change`.
- *Fundamentals (A0):* `debt_to_equity, cash_to_marketcap, beta, profit_margin, log_market_cap, sector(cat)`.
- *Cross-sectional within world:* `runup_rank, size_rank`.
- *Old-model as features (Liran G=yes):* `ml_class_prob, ml_pred_peak, ml_dir, momentum_roc`.
Validation: row count == valid observations; no NaN in required features; median `|y_hedged|` ≈ 3–6%.

**A3 — Model 1 (alpha).** XGBoost regressor on `y_hedged` (Liran: RF or XGBoost — default XGBoost;
confirm). **Loss = Huber** (robust to ±40% earnings outliers). Regularize hard: `max_depth 3–4,
min_child_weight ≥ 5, subsample 0.7, colsample 0.7`, early-stop on `val`. Fill `alpha_score`.
- **Train on `train` only; tune on `val`; never touch `test` until final.**
- **Success criterion (report OOS on `test`):** rank-corr(`alpha_score`,`y_hedged`) > 0 **and**
  top-quintile-by-`alpha_score` has positive mean `y_hedged`. If not, there is no per-trade edge —
  tell Liran; do not paper over it.
- Ablate feature groups (does each add OOS rank-corr?). Report in-sample-vs-OOS gap (overfit tell).

## Decisions baked in (Liran)
Split = chronological only (verified real-world); fundamentals = pull what yfinance has + flag the
point-in-time caveat; universe = produce **all** archetypes (Agent B/Model-2 filters; also emit a
boolean `pre_drop_earnings_defense` so B can test the pre-dropped universe); old-ML+momentum = include
as features; horizon for the label = 10 trading days (Agent B owns live exits).

## Guardrails
Purged walk-forward + 10-day embargo; OOS is the only score; linear baseline before trees; assume any
in-sample gain is fake until OOS confirms (we lived 0.67 IS → 0.39 OOS); n≈1,733 — stay humble.

## Definition of done
`build_dataset.py` reproducibly writes `candidates.parquet` matching the contract; A0 table populated
with a coverage report; Model 1 trained with the OOS edge report; a short `ALPHA_RESULTS.md`. Coordinate
the contract schema and any change to it with Agent B in the PR description.
