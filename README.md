# Arbitraging the Information Diffusion Lag
### A Prediction-Market-Anchored Framework for Cross-Sectional Equity Mispricings

> **Status: Backtesting Phase** — Core pipeline architecture is implemented. Active backtesting of ML engine and momentum strategy is underway using a leakage-safe RF experiment runner.

---

## Overview

This system exploits the structural lag between **prediction market probability shifts** and the **cross-sectional repricing of individual equities** around macro and geopolitical events.

When a macro shock occurs (e.g., a Fed rate cut), HFT algorithms and ETF arbitrageurs instantly reprice broad indices — forcing all S&P 500 constituents to move in lockstep regardless of their individual fundamentals. This non-fundamental comovement creates a temporary mispricing window that corrects slowly as firm-specific capital flows in. We use [Polymarket](https://polymarket.com) as a real-time oracle to detect when this gap is opening — **before** the cross-sectional correction begins.

---

## Evaluation Architecture

Our backtesting architecture now relies on a leakage-safe Random Forest (RF) experiment runner (`run_experiments_rf.py`) with a rigorous evaluation structure separated into four distinct roles:

1. **CEM Training Diagnostic**: Cross-Entropy Method (CEM) search only sees causally available training labels and train-period paths.
2. **Static OOS Validation**: One RF + one CEM policy are frozen at the start of the pre-terminal test period and evaluated without any retraining.
3. **Expanding Online Walk-Forward Diagnostic**: At each evaluation window, RF and CEM are re-fit using only labels that were available before that window.
4. **Final Terminal Holdout**: A final chronological segment of the original test split is never used by RF fitting, CEM, policy selection, or online walk-forward refitting.

---

## Core Modeling & Simulation Assumptions

(Derived from `run_experiments.py` and `run_experiments_rf.py`)

* **No Lookahead Bias**: Every fit/evaluation simulation is valuated at its own cutoff date; it cannot use prices after that cutoff. Finite-horizon simulations truncate price/probability paths before trade generation, so entry/exit decisions cannot inspect later observations.
* **Causal Purging**: We use causal expanding, purged OOF RF predictions without standard KFold cross-validation to prevent leakage. The label-availability timestamp controls training eligibility.
* **Strict OOS Metrics**: OOS metrics come from a separate, frozen-policy portfolio simulation on test candidates only, with one fixed OOS end date across all ablations. They are actual portfolio returns, not summed trade P&L.
* **Fully Net P&L**: Trade P&L is fully net of all modeled rotation costs: `benchmark sell + asset buy + asset sell + benchmark rebuy`.
* **Objective Function**: The CEM objective uses daily portfolio-equity Sharpe, not a small sample of trade returns.
* **Deterministic Seeds**: Each experiment starts CEM from the same benchmark-specific random seed, so an ablation is not confounded by a different initial population.

## Ablations and Enhancements
We systematically evaluate three major ablations to ensure the edge is genuine:
* **T1 (Friction Hurdle):** An entry-only ex-ante friction hurdle. The trade must have predicted gross edge >= HURDLE_MULT * estimated all-in benchmark rotation costs.
* **T2 (Train Windows):** Internal train-only rolling CEM evaluation windows.
* **T3 (Kelly):** Half-Kelly implementation using fully net realised returns and reporting actual realised sizing.

---

## Repository Structure

```text
my_traders/
├── main_backtesting/          # Backtesting framework (active)
├── pipeline/                  # Strategy implementations and execution
├── database/                  # PostgreSQL schema + ORM layer
├── general_testing/           # Misc experiments
├── run_experiments.py         # Baseline CEM optimization & simulation runner
├── run_experiments_rf.py      # Leakage-safe RF experiment runner (Working Model)
└── README.md                  # This file
```

---

## Performance Metrics (Target)

- **Total Return** — net of transaction costs and rejected trades
- **Sharpe Ratio** — vs. buy-and-hold, momentum baseline, and no-signal ML baseline
- **Maximum Drawdown** — worst peak-to-trough loss
- **Hit Rate** — share of trades with correct direction and positive P&L

---

*Author: Liran Attar — MSc CS, Ben-Gurion University*
