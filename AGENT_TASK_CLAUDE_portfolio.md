# AGENT TASK — Track B: Portfolio Layer + backtester  (CLAUDE CODE)

You are **Agent B**. A second agent (**Agent A / Codex**) builds the data + alpha model in parallel
on another branch. You two meet at **one contract** (the `candidates` dataset, below). Agent A
produces it; you consume it. **You can mock `alpha_score` and start immediately** — don't wait for A.

**Branch:** `model/portfolio-layer` (off `master`). **Repo:** `C:\Users\Liran\PycharmProjects\my_traders`,
venv `.venv\Scripts\python.exe`, Postgres schema `checking_relevant_events`
(`database/db_connection.py`). **Read first:** `WORKING_WITH_LIRAN.md`, `MODEL_PLAN.md`.

## Shared context (read, do not re-derive)
- Strategy: a Polymarket probability crossing 55% flags an equity mispricing during the
  information-diffusion lag; trade the exposed names over that window.
- **Proven (treat as given):** the lag is real; **direction is the binding constraint**; the real
  target is the **sector-hedged (idiosyncratic)** return (raw is ~89% beta); the Polymarket
  cross-back-down exit helps; sentiment is excluded.
- **Two-model architecture.** Model 1 (Agent A) = alpha (per-asset long/short at the crossing).
  *You build Model 2* = the **Portfolio Layer**: every iteration, for each asset held or just
  suggested by the RF, choose **{buy, sell, hold}**; **allocation per position is CONSTANT**; it owns
  **entry and exit dynamically**; objective = **maximize Sharpe OR minimize max-drawdown** (config).

## ★ THE CONTRACT (must match Agent A exactly) ★
Agent A writes `data/candidates.parquet` (one row per candidate). You read it + the raw series from
the DB. Columns you rely on: `run_id, event_id, market_id, symbol, t0, t_theta, t_e, entry_price,
sector_etf, y_hedged, realized_dir, alpha_score, alpha_dir, split, pre_drop_earnings_defense`, plus the
`feat_*` block. **Until A delivers, mock `alpha_score`** (e.g. `realized_dir`-leaked for plumbing
tests, then random for honest dry-runs). Per candidate you pull from the DB for `[t_theta, t_e]`:
- price path: `historical_price_bars(symbol, '1d')` and `historical_price_bars(sector_etf, '1d')`,
- probability path: `historical_probability_points(market_id)` (hourly → daily).
Lock this schema with Agent A before building the executor.

## Your tasks
**B0 — backtest harness.** A clean simulator that walks **day by day** over each candidate's life
`[entry … t_e]`, holding the `$100k` book, applying the Portfolio Layer policy, and recording the
**daily portfolio return series** → from which you compute Sharpe (annualized), max-drawdown, Calmar,
net %, hit rate, turnover. **Each position is sector-hedged** (long asset / short `sector_etf`, or the
reverse), constant notional. Reuse helpers/patterns from `general_testing/idea_backtests.py`.

**B1 — Portfolio Layer v1 (rule-based, the robust floor).** Per iteration, per asset:
- **ENTRY (with the confirmation band — Liran wants this swept):** act on `alpha_dir` only when the
  crossing is confirmed. A crossing **≥ `entry_strong_probability`** enters immediately; a crossing in
  **`[0.55, entry_strong_probability)`** must **hold above 0.55 for the confirmation window** first.
  **Sweep `entry_strong_probability ∈ {0.60, 0.65, 0.70}` × confirmation window ∈ {2 days}.** (This is
  the existing `entry_strong_probability` / `entry_confirmation_hours` logic; prior 2h/0.60 test hurt
  in the old long-only book — re-measure here, don't assume.)
- **EXIT toolbox (sweep the stop type — Liran's question):**
  1. **Polymarket reversal:** sell if probability crosses back below `θ_out` (sweep `θ_out ∈ {0.50,0.55}`).
  2. **Stop loss:** sweep **{fixed %, fixed-% trailing (no ATR), close-volatility trailing}** ×
     level. (Empirically the close-vol trailing stop was best, +4.98%, vs the 3% fixed that bled —
     but test all.)
  3. **Resolution exit:** out 1 trading day before `t_e` (the event-edge's natural end). **No fixed
     10-day cap** — horizon = the event's own resolution.
  4. (optional) **RF flip** if Agent A later emits per-iteration scores.
- **HOLD** otherwise. Tune the policy thresholds on `train`, validate on `val`, report on `test`.

**B2 — objective sweep (Liran A=one-by-one).** Run the v1 tuning for **Sharpe**, then **Calmar**, then
**return-subject-to-max-drawdown**, separately. Report each.

**B3 — universe (Liran E=both).** Run on the full archetype set *and* on the `pre_drop_earnings_defense`
subset; compare. (Liran expects all-archetypes to win — verify.)

**B4 — Portfolio Layer v2 (learned policy — frontier, after v1).** Per asset per step output `P(hold)`;
**reward = Sharpe (or −maxDD)** of the daily portfolio path; train via RL (policy gradient) or a
differentiable path, with turnover/gross/sector-concentration penalties. 3-action space + constant size
keep it tractable, but it overfits hard on ~1,733 candidates — **guilty until OOS**, and **only after
v1 sets a floor and Agent A's Model 1 shows real per-trade edge.** Do not start B4 before that.

## Baselines v1/v2 must beat (OOS)
Equal-weight all confirmed crossings; long-oil-on-escalation only; the current ML book; random hold/sell.

## Decisions baked in (Liran)
Constant position size; chronological split (from the contract's `split`); exit owned by this layer
(dynamic, not a fixed rule); entry-band swept (0.60/0.65/0.70, 2-day window); RF scores once at the
crossing (rules run each iteration) unless Agent A enables re-scoring.

## Guardrails
Tune on train, pick on val, report on test — never peek. OOS Sharpe is the headline; always print the
IS-vs-OOS gap. Start every learned thing with the simplest rule baseline. n≈1,733 — humility.

## Definition of done
A reproducible backtester reading the contract; v1 results across the entry-band × stop-type ×
objective × universe sweeps with OOS Sharpe/maxDD tables vs baselines; a short `PORTFOLIO_RESULTS.md`.
Coordinate any contract-schema need with Agent A in the PR description.
