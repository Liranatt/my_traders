"""
RL agent / PPO / environment configuration.

Why long-only (do not change without re-reading EXPERIMENTS_LOG.md E4.7):
    The RF's out-of-sample directional hit rate is ~48-50% (a coin flip) and its
    prediction rank-correlation collapses from ~0.9 in-sample to ~-0.07 OOS.
    "Long the structural beneficiary" beat "RF-picks-direction" by ~14pp OOS.
    So the agent never shorts: BUY = go long the mapped beneficiary once the
    Polymarket entry band fires; the learnable value is entry-confirmation
    timing, the daily HOLD-vs-SELL exit, and sizing. Direction is NOT learned.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import NamedTuple


# Portfolio action space. The agent can stay in the benchmark, enter a trade,
# or exit an open trade back into the benchmark. Learn entry timing first;
# sizing can come back later once entry/exit behavior is stable.
POSITION_SIZE_CHOICES: tuple[float, ...] = (0.10,)
POLY_EXIT_THRESHOLD: float = 0.55
BENCHMARKS: tuple[str, ...] = ("SPY", "QQQ")
EXIT_THRESHOLD_GRID: tuple[float, ...] = (0.50, 0.55, 0.60, 0.65, 0.70)


class EntryActionParams(NamedTuple):
    position_size_pct: float


ENTRY_ACTION_PARAMS: tuple[EntryActionParams, ...] = tuple(
    EntryActionParams(size) for size in POSITION_SIZE_CHOICES
)


ACTION_HOLD = 0
ACTION_ENTER_OFFSET = 1
ACTION_EXIT = ACTION_ENTER_OFFSET + len(ENTRY_ACTION_PARAMS)
ACTION_DIM = ACTION_EXIT + 1
ENTER_ACTIONS: tuple[int, ...] = tuple(
    range(ACTION_ENTER_OFFSET, ACTION_ENTER_OFFSET + len(ENTRY_ACTION_PARAMS))
)


def is_enter_action(action: int) -> bool:
    return action in ENTER_ACTIONS


def position_size_for_action(action: int) -> float:
    return entry_params_for_action(action).position_size_pct


def entry_params_for_action(action: int) -> EntryActionParams:
    if not is_enter_action(action):
        raise ValueError(f"Action {action} is not an ENTER action.")
    return ENTRY_ACTION_PARAMS[action - ACTION_ENTER_OFFSET]


def action_for_entry_params(position_size_pct: float) -> int:
    params = EntryActionParams(position_size_pct)
    try:
        return ACTION_ENTER_OFFSET + ENTRY_ACTION_PARAMS.index(params)
    except ValueError as exc:
        raise ValueError(f"Unsupported entry action parameters: {params}") from exc


# Fixed observation layout (order matters; the scaler and network depend on it).
OBSERVATION_COLS: list[str] = [
    # static candidate features (RF signal + a few lean structural features)
    "rf_pred_decimal",
    "rf_pred_rank",
    "feat_prob_at_trigger",
    "feat_time_to_resolution_days",
    "feat_spy_2w_trend",
    "feat_debt_to_equity",
    "feat_asset_2w_trend",
    "feat_runup_since_t0",
    "feat_pre_entry_volume_log",
    "feat_beta",
    "feat_log_market_cap",
    "feat_connection_strength",
    "feat_prob_slope_24h",
    "feat_prob_surge_since_t0",
    "feat_crossing_latency_days",
    "archetype_is_earnings",
    "feat_llm_expected_return",
    "expected_return_pct",
    "confidence_score",
    "feat_llm_confidence",
    "llm_target",
    "llm_confidence_norm",
    # live position state
    "window_fraction_elapsed",
    "unrealized_ret",
    "peak_ret",
    "drawdown_from_peak",
    "position_size_pct",
    "convergence_residual",
    "profit_vs_expectation",
    "time_decay_conviction",
    # live market state
    "current_prob",
    "prob_slope_3d",
    "bench_trend_5d",
]
# Indices of the RF-derived observation dims (zeroed when the RF skill gate fails).
RF_OBS_COLS: tuple[str, ...] = ("rf_pred_decimal", "rf_pred_rank")


@dataclass
class RLConfig:
    """Configuration for the PPO agent and the training/eval protocol."""

    # --- PPO hyperparameters ---
    actor_lr: float = 5e-5  # Lowered significantly to stop wild swings
    critic_lr: float = 3e-4  # Keep at 3e-4 so it accurately predicts drawdowns fast
    gamma: float = 1.0
    gae_lambda: float = 0.95
    clip_epsilon: float = 0.15  # Middle ground (allows decent sized updates)
    entropy_beta: float = 0.02  # Encourage exploration without relying on architectural noise.
    weight_decay: float = 1e-4
    n_epochs: int = 10
    n_minibatches: int = 6  # Middle ground (smoother than 4, faster than 8)
    max_train_epochs: int = 60
    patience: int = 25  # early-stopping patience on validation Sharpe
    teacher_warmup_epochs: int = 10
    teacher_bc_epochs: int = 2
    teacher_batch_size: int = 512

    # --- network ---
    actor_hidden_dims: list[int] = field(default_factory=lambda: [16, 16])
    critic_hidden_dims: list[int] = field(default_factory=lambda: [16, 16])

    # --- environment / portfolio ---
    base_position_size: float = 0.10
    position_size_choices: tuple[float, ...] = POSITION_SIZE_CHOICES
    poly_exit_threshold: float = POLY_EXIT_THRESHOLD
    max_concurrent: int = 10
    annualization: float = 252.0  # trading days (matches daily_equity_sharpe)

    # --- training protocol ---
    outer_seeds: tuple[int, ...] = (42, 43, 44)
    benchmarks: tuple[str, ...] = BENCHMARKS
    exit_threshold_grid: tuple[float, ...] = EXIT_THRESHOLD_GRID
    default_exit_threshold: float = 0.60

    # --- RF skill gate (drop the RF signal if it cannot beat a coin flip) ---
    min_rf_skill_hit_rate: float = 0.52
    min_rf_skill_rank_ic: float = 0.02

    @property
    def obs_dim(self) -> int:
        return len(OBSERVATION_COLS)

    @property
    def action_dim(self) -> int:
        return ACTION_DIM
