# my_traders

A Polymarket-driven, long-only equity trading system. It scans active prediction markets, uses Gemini to identify mechanically-exposed US-listed stocks and ETFs, and then deploys a **PPO-trained reinforcement learning agent** to decide when to enter, how much to size, and when to exit each trade. The RL agent is the primary trading decision-maker; the rule-based pipeline components exist for data production, feature engineering, and research tooling.

---

## How It Works — Big Picture

```
Polymarket API
      │
      ▼
  LLM Layer (Gemini)
  ┌─────────────────────────────────────────┐
  │  Pass 1: Relevance Gate                 │
  │    Score each question 0–1:             │
  │    how mechanically does YES reprice    │
  │    US equities? Drop anything < 0.10.  │
  │                                         │
  │  Pass 2: Tight Asset Mapping            │
  │    Reason channel → instruments →       │
  │    connection_strength.                 │
  │    Validate every ticker against        │
  │    IB security master.                  │
  └─────────────────────────────────────────┘
      │
      ▼
  Feature Engineering
  (21 numerical + 2 categorical features per
   market × symbol pair, incl. probability
   slope, ATR, fundamentals, cross-sectional
   rank, SPY/sector trend)
      │
      ▼
  RL Agent (PPO)  ◄── THE MAIN EVENT
  ┌─────────────────────────────────────────┐
  │  RF Gate: filters noise before RL sees  │
  │  Teacher BC: warm-start from rule-based │
  │  PPO: optimises benchmark-relative      │
  │       excess return (long-only)         │
  │  Exit threshold: grid-searched on val   │
  └─────────────────────────────────────────┘
      │
      ▼
  IBGateway (live execution)
```

---

## Repository Layout

```
my_traders/
│
├── main.py                   # Entry point: scan | backtest | live
│
├── LLM/
│   └── build_world.py        # Pydantic schemas + Gemini prompt templates
│                               (two-pass: relevance gate → tight mapping)
│
├── pipeline/
│   ├── scanner.py            # Hits Polymarket Gamma API, deduplicates vs DB
│   ├── evaluator.py          # Orchestrates the two-pass Gemini pipeline
│   ├── data_loader.py        # Builds candidates.parquet; 21+2 features
│   ├── strategy.py           # Rule-based baseline policy (NOT the main agent)
│   ├── portfolio_manager.py  # CEM policy search + split evaluation
│   ├── model.py              # Random Forest alpha filter (research use)
│   └── backtest.py           # Backtest harness (rule-based path)
│
├── rl/                       # ← PRIMARY TRADING LOGIC
│   ├── config.py             # RLConfig dataclass (all hyperparameters)
│   ├── env.py                # Gym-style TradingEnv (one episode = one candidate)
│   ├── features.py           # Observation builder + RF skill gate
│   ├── exits.py              # Hard-exit rules (ATR stop, prob drop, resolution cap)
│   ├── teacher.py            # Builds BC warm-start examples from rule-based policy
│   ├── policy.py             # PolicyNetwork (actor-critic, masked softmax)
│   ├── ppo.py                # PPO update loop (GAE, clipped objective, value loss)
│   ├── train.py              # Full training pipeline (RF gate → teacher BC → PPO)
│   ├── sim_with_policy.py    # Portfolio-level simulation with a loaded policy
│   ├── evaluate.py           # Terminal holdout evaluation
│   ├── reward.py             # Reward utilities
│   ├── shared.py             # Shared helpers (calendar, IB cost, data partitioning)
│   └── tests/                # Unit tests
│
├── database/
│   └── backtesting/schema.py # Postgres schema (historical_price_bars,
│                               historical_probability_points, etc.)
│
├── run_experiments.py        # Large-scale RL hyperparameter sweep
├── run_experiments_rf.py     # RF-layer experiment runner
├── run_opp_cost.py           # Opportunity-cost analysis
├── perplexity_calling.py     # Perplexity API integration (news enrichment)
│
└── data/
    ├── candidates.parquet    # Feature-engineered candidate dataset
    ├── backtest_trades.csv   # Output of backtest runs
    └── rl_experiment_checkpoints/
        ├── rl_policy_{bench}_{seed}.pt        # Saved policy weights
        └── rl_policy_{bench}_{seed}.meta.json # Scaler, RF flag, exit threshold
```

---

## The RL Agent in Detail

### Environment (`rl/env.py`)

Each episode covers a single (market, symbol) candidate, from the day its entry signal fires to one day before resolution (`t_e`). The environment steps over the IB trading calendar.

**Action space** (masked per step):

| Action | Available when |
|--------|----------------|
| `HOLD` | Flat before signal, or Long before hard exit |
| `ENTER` (×N sizes) | Flat, after entry signal fires, before resolution cap |
| `EXIT` | Long, any time (forced on hard exit or final day) |

**Reward:** Daily benchmark-relative excess return, scaled ×100 and clipped to ±10. IB transaction costs (per-share commissions) are deducted at entry and exit. The benchmark is configurable (default: SPY).

**Hard exits** (`rl/exits.py`): The environment can force an `EXIT` independently of the policy when any of the following trigger:
- ATR trailing stop breached
- Polymarket YES probability drops below the exit threshold
- Resolution day –1 (mandatory liquidation)
- Profit lock floor (activated after a configurable gain)

### Feature Observation

The observation vector has three components, assembled in `rl/features.py`:

1. **Static features** — normalised at training time via a per-column min-max scaler stored in `.meta.json`. Includes: probability at trigger, probability slope/volatility, time-to-resolution, `connection_strength`, ATR, 2-week asset/SPY/sector trends, beta, debt-to-equity, profit margin, market cap, cross-sectional rank features, RF score (when RF gate is active).

2. **Position state** — is_long flag, unrealised excess return vs entry, drawdown from peak, time elapsed in trade, position size %.

3. **Market state** — current probability, probability delta from yesterday, current asset/bench prices and their daily returns.

### Policy Network (`rl/policy.py`)

Actor-critic network with configurable hidden dimensions. The actor outputs raw logits; illegal actions are masked with −∞ before softmax so the policy never samples them. The critic outputs a scalar value estimate for GAE computation.

### Training Pipeline (`rl/train.py`)

Training runs per benchmark symbol (e.g., SPY) and per random seed for ensemble diversity.

**Step 1 — RF Skill Gate:** A Random Forest is trained on development data to predict trade profitability. `compute_rf_skill` evaluates hit rate and rank IC on a causal holdout. If both metrics clear the configured thresholds, RF predictions are added as features and used to gate which candidates the RL agent trains on.

**Step 2 — Teacher Behavioral Cloning:** `rl/teacher.py` generates (observation, action, mask) tuples by running the rule-based strategy on training candidates. The PPO object's `update_teacher` method performs supervised BC on these examples for `teacher_warmup_epochs` epochs. This gives the policy a sensible starting point so early PPO rollouts are not purely random.

**Step 3 — PPO:** Standard clipped-surrogate PPO with GAE(λ) advantage estimation. Entropy coefficient is annealed from `entropy_beta` → 0 over the first ~15 PPO epochs to encourage early exploration and later exploitation. Each epoch collects full rollouts over all training environments before a single update step.

**Step 4 — Validation + Exit Threshold Search:** After every epoch, `_select_exit_threshold` runs `sim_with_policy` on the validation set across a grid of probability exit thresholds and picks the one that maximises excess return. The best policy checkpoint and its exit threshold are saved to disk when validation score improves.

**Early stopping:** Training halts after `patience` epochs without improvement in validation excess return.

### Portfolio Simulation (`rl/sim_with_policy.py`)

`sim_with_policy` runs the saved policy over a set of candidates, handling concurrent position limits and optional Kelly-fraction sizing. It returns per-trade records and aggregate statistics (excess return, Sharpe, max drawdown, win rate, trade count).

### Evaluation (`rl/evaluate.py`)

Terminal holdout evaluation is strictly separated from training. The static eval partition is never touched during training; `rl/evaluate.py` owns all reporting on it.

---

## Data Flow

```
Polymarket Gamma API
        │
        ▼
pipeline/scanner.py  ──►  database (new markets)
        │
        ▼
pipeline/evaluator.py
  + LLM/build_world.py  ──►  database (asset worlds per market)
        │
        ▼
pipeline/data_loader.py  ──►  data/candidates.parquet
        │                      (one row per market × symbol)
        ▼
rl/train.py  ──►  data/rl_experiment_checkpoints/
        │          rl_policy_{bench}_{seed}.pt
        │          rl_policy_{bench}_{seed}.meta.json
        ▼
rl/evaluate.py  (terminal holdout reporting)
        │
        ▼
main.py live  ──►  IBGateway (orders)
```

---

## Running the System

### Prerequisites

- Python 3.11+
- PostgreSQL (schema in `database/backtesting/schema.py`)
- IB Gateway (for live mode)
- Gemini API key
- Polymarket Gamma API access

### Install

```bash
pip install -r requirements.txt
```

### Scan and Evaluate New Markets

```bash
python main.py scan
```

Hits the Polymarket API, runs the two-pass Gemini evaluation pipeline, and stores new market → asset world mappings in the database.

### Build the Feature Dataset

```bash
python -m pipeline.data_loader
```

Builds `data/candidates.parquet` from the database.

### Train the RL Agent

```bash
python -m rl.train
```

Reads `data/candidates.parquet`, trains one PPO policy per benchmark × seed combination, and writes checkpoints to `data/rl_experiment_checkpoints/`.

### Evaluate on Terminal Holdout

```bash
python -m rl.evaluate
```

Loads all checkpoints and reports performance on the static eval partition (never touched during training).

### Run Hyperparameter Experiments

```bash
python run_experiments.py       # RL sweep
python run_experiments_rf.py    # RF layer sweep
```

### Backtest (Rule-Based Baseline)

The rule-based pipeline backtest is available for comparison:

```bash
python main.py backtest             # default parquet
python main.py backtest --from-db   # rebuild from DB first
python main.py backtest --rl        # CEM policy search
python main.py backtest --rf        # add RF filter
```

### Live Trading

```bash
python main.py live           # connect to IBGateway
python main.py live --paper   # paper account
```

---

## Configuration

All RL hyperparameters are centralised in `rl/config.py` via the `RLConfig` dataclass:

| Parameter | Description |
|-----------|-------------|
| `benchmarks` | List of benchmark symbols (default: `["SPY"]`) |
| `outer_seeds` | Random seeds for ensemble training |
| `obs_dim` | Observation vector dimension |
| `actor_hidden_dims` | Hidden layer sizes of the policy network |
| `action_dim` | Total number of discrete actions |
| `position_size_choices` | Discrete position sizes available to the agent |
| `base_position_size` | Default size for simulation |
| `max_concurrent` | Max simultaneous open positions in portfolio sim |
| `max_train_epochs` | Maximum PPO training epochs |
| `teacher_warmup_epochs` | Epochs of BC warm-start before PPO begins |
| `teacher_bc_epochs` | Gradient steps per teacher update |
| `entropy_beta` | Initial entropy coefficient (annealed to 0) |
| `patience` | Early stopping patience (epochs without improvement) |
| `default_exit_threshold` | Default probability threshold for exits |
| `exit_threshold_grid` | Grid of thresholds searched at each val epoch |
| `min_rf_skill_hit_rate` | RF gate: minimum hit rate to activate RF features |
| `min_rf_skill_rank_ic` | RF gate: minimum rank IC to activate RF features |

---

## Key Design Decisions

**Benchmark-relative reward.** The environment rewards excess return over SPY (or a configured benchmark), not raw return. This prevents the agent from learning "always be long during bull markets" as a trivially profitable strategy, and forces it to find real alpha from the Polymarket signal.

**Action masking.** The policy network never sees illegal actions. Masks are computed per step based on state (flat/long) and signal timing, eliminating the need for penalty shaping around invalid actions.

**Teacher warm-start.** Cold-starting PPO on financial time-series data with sparse rewards is extremely slow. The BC phase bootstraps the policy with the rule-based strategy's behaviour, dramatically reducing the number of PPO epochs needed to reach competent performance.

**RF gate as a pre-filter.** The Random Forest is not part of the RL observation in all configurations — it is an upstream noise filter. If RF skill metrics don't clear the thresholds, the gate is disabled entirely and the RL agent trains on the full candidate set. This prevents RF from degrading RL performance when it has no predictive signal.

**Leakage-safe partitioning.** Training uses only candidates whose resolution date (`t_e`) falls strictly before the start of the static eval window. The static eval partition is never touched during any training or hyperparameter search step; only `rl/evaluate.py` reads it.

**Checkpoint + meta.json.** Every saved policy comes with a JSON sidecar containing the fitted scaler, RF flag, observation column list, action dimension, and selected exit threshold. Loading a checkpoint for inference requires only the `.pt` + `.meta.json` pair — no dataset rebuild needed.

---

## Components That Are Research/Baseline Only

The following modules are not part of the live RL trading path. They exist for research, comparison, and dataset production:

- `pipeline/strategy.py` — rule-based baseline (ATR stop, probability threshold, profit lock). Used as teacher signal source and backtest comparison.
- `pipeline/portfolio_manager.py` — CEM (Cross-Entropy Method) policy search over the rule-based strategy parameters.
- `pipeline/model.py` — standalone Random Forest alpha filter experiment.
- `pipeline/backtest.py` — backtest harness for the rule-based strategy path.
- `run_experiments_rf.py` — RF hyperparameter sweep.
- `run_opp_cost.py` — opportunity cost analysis.
