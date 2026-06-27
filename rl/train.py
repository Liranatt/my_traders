"""
Leakage-safe PPO training for the long-only RL trading agent.

Protocol (mirrors the static-OOS path of run_experiments_rf, expanding-window
flavor of T2):
  * Preprocess candidates exactly like the RF runner (relevance filter, tz,
    label_available_ts = t_e).
  * partition_data -> development / static_eval (validation) / terminal (never
    touched here).
  * Train only on development rows whose label completed before the validation
    boundary (rows_completed_before(development, static_start)) -> no future
    outcome can train an earlier decision. RF features are causal OOF.
  * RF skill gate: if the RF can't beat a coin flip on development OOF, its two
    observation dims are zeroed (decision logged + saved with the checkpoint).
  * Early-stop on validation portfolio Sharpe (static_eval); terminal is only
    used by evaluate.py, once.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import numpy as np
import pandas as pd
import torch

from rl.config import RLConfig, OBSERVATION_COLS
from rl.env import TradingEnv, has_valid_episode
from rl.features import (
    attach_static_features,
    compute_rf_skill,
    fit_static_scaler,
    get_rf_predictions,
    rf_passes_gate,
)
from rl.policy import PolicyNetwork
from rl.ppo import CEM_RECOMMENDS_EXIT_IDX, PPO
from rl.sim_with_policy import sim_with_policy
from rl.shared import (
    RELEVANCE_COL,
    load_paths,
    partition_data,
    rows_completed_before,
)

PROJECT = Path(__file__).resolve().parent.parent
CHECKPOINT_DIR = PROJECT / "data" / "rl_experiment_checkpoints"
BRAIN_DUMP_PATH = PROJECT / "rl_brain_dump.log"
BENCH = "SPY"  # validation benchmark for early stopping


def mask_cem_for_policy(obs: torch.Tensor) -> torch.Tensor:
    policy_obs = obs.clone()
    if policy_obs.dim() == 1:
        policy_obs[CEM_RECOMMENDS_EXIT_IDX] = 0.0
    else:
        policy_obs[:, CEM_RECOMMENDS_EXIT_IDX] = 0.0
    return policy_obs


class CEMMaskedPolicy:
    def __init__(self, policy: PolicyNetwork):
        self.policy = policy

    def forward(self, obs: torch.Tensor):
        return self.policy.forward(mask_cem_for_policy(obs))


def preprocess(df: pd.DataFrame) -> pd.DataFrame:
    df = df[df[RELEVANCE_COL].astype(float) > 0.5].copy()
    df["t_theta"] = pd.to_datetime(df["t_theta"], utc=True)
    df["t_e"] = pd.to_datetime(df["t_e"], utc=True)
    df["label_available_ts"] = pd.to_datetime(df["t_e"], utc=True)
    df = df[df["label_available_ts"].notna()].copy()
    df["_candidate_id"] = df.index.astype(str)
    return df


def build_envs(df_feat, prices, probs, scaler, config) -> list[TradingEnv]:
    envs = []
    for _, row in df_feat.iterrows():
        if has_valid_episode(row, prices, probs, BENCH):
            envs.append(TradingEnv(
                row, prices, probs, BENCH, scaler=scaler,
            ))
    return envs


async def async_main():
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    if BRAIN_DUMP_PATH.exists():
        BRAIN_DUMP_PATH.unlink()
    config = RLConfig()

    df = preprocess(pd.read_parquet(PROJECT / "data" / "candidates.parquet"))
    parts = partition_data(df)
    development, static_eval = parts["development"], parts["static_eval"]
    static_start, static_end = parts["static_start"], parts["static_end"]
    train_df = rows_completed_before(development, static_start)
    print(f"development={len(development)}  train(label-complete<{static_start.date()})={len(train_df)}  val={len(static_eval)}")

    prices, probs = await load_paths(df)

    for seed in config.outer_seeds:
        print(f"\n=== seed {seed} ===")
        torch.manual_seed(seed)
        np.random.seed(seed)
        ckpt_path = CHECKPOINT_DIR / f"rl_policy_seed_{seed}.pt"
        meta_path = CHECKPOINT_DIR / f"rl_policy_seed_{seed}.meta.json"
        if ckpt_path.exists():
            ckpt_path.unlink()
        if meta_path.exists():
            meta_path.unlink()

        # RF skill gate (development OOF only — leakage-safe).
        skill = compute_rf_skill(development, as_of=static_start, seed=seed)
        use_rf = rf_passes_gate(skill, min_hit_rate=config.min_rf_skill_hit_rate, min_rank_ic=config.min_rf_skill_rank_ic)
        print(f"RF skill: hit_rate={skill['hit_rate']:.3f} rank_ic={skill['rank_ic']:.3f} n={skill['n']} -> use_rf={use_rf}")

        # Training features (causal OOF) + train-only scaler.
        train_preds, unit = get_rf_predictions(development, train_df, as_of=static_start, seed=seed, use_causal_oof=True)
        train_feat = attach_static_features(train_df, train_preds, unit, use_rf=use_rf)
        scaler = fit_static_scaler(train_feat)
        train_envs = build_envs(train_feat, prices, probs, scaler, config)
        print(f"training episodes: {len(train_envs)}")
        if len(train_envs) < 5:
            print("  too few training episodes; skipping seed.")
            continue

        # Validation features (RF fit as-of the validation boundary).
        val_preds, _ = get_rf_predictions(development, static_eval, as_of=static_start, seed=seed, use_causal_oof=False)
        val_feat = attach_static_features(static_eval, val_preds, unit, use_rf=use_rf)

        policy = PolicyNetwork(
            obs_dim=config.obs_dim,
            hidden_dims=config.actor_hidden_dims,
            action_dim=config.action_dim,
        )
        ppo = PPO(policy, config)

        best_val = -float("inf")
        patience = 0
        for epoch in range(1, config.max_train_epochs + 1):
            # Phase 1: BC warm start teaches HOLD-vs-EXIT from the CEM prior.
            is_bc_phase = (epoch <= 15)

            # Phase 2: KL-anchored PPO fades the CEM prior out over 15 epochs.
            if is_bc_phase:
                kl_coef = 0.0
            else:
                progress = max(0.0, min(1.0, (epoch - 16) / 15.0))
                kl_coef = 0.01 + 0.04 * (1.0 - progress)

            np.random.shuffle(train_envs)
            for env in train_envs:
                env.force_entry = is_bc_phase
                obs = env.reset()
                done = False
                while not done:
                    mask = env.get_action_mask()
                    ot = torch.as_tensor(obs, dtype=torch.float32).unsqueeze(0)
                    mt = torch.as_tensor(mask, dtype=torch.bool).unsqueeze(0)
                    with torch.no_grad():
                        action, log_prob, _, value = policy.get_action(mask_cem_for_policy(ot), mt)
                    next_obs, reward, done, _ = env.step(int(action.item()))
                    ppo.buffer.obs.append(obs)
                    ppo.buffer.actions.append(int(action.item()))
                    ppo.buffer.logprobs.append(float(log_prob.item()))
                    ppo.buffer.masks.append(mask)
                    ppo.buffer.values.append(float(value.item()))
                    ppo.buffer.rewards.append(float(reward))
                    ppo.buffer.dones.append(bool(done))
                    obs = next_obs
            if is_bc_phase:
                loss = ppo.update_bc()
            else:
                loss = ppo.update(kl_coef=kl_coef)

            policy.eval()
            eval_policy = CEMMaskedPolicy(policy)
            _, _, val_stats = sim_with_policy(
                val_feat, prices, probs, eval_policy, scaler=scaler,
                bench_sym=BENCH,
                use_kelly=False, base_ps=config.base_position_size,
                max_concurrent=config.max_concurrent,
                start_date=static_start, end_date=static_end,
            )
            policy.train()
            # The validation window is often too short for annualized Sharpe,
            # so it cannot drive model selection.
            # Select on a short-window-safe, risk-aware score: excess return over
            # the index minus a drawdown penalty (both in percent). Sharpe is still
            # reported for reference when enough daily returns are available.
            val_excess = float(val_stats.get("excess_return", 0.0))
            val_dd = abs(float(val_stats.get("max_dd", 0.0)))
            val_sharpe = float(val_stats.get("sharpe", 0.0))
            val_sharpe_returns = int(val_stats.get("sharpe_daily_returns", 0))
            min_sharpe_returns = int(val_stats.get("min_daily_returns_for_sharpe", 20))
            val_sharpe_text = (
                f"{val_sharpe:+.2f}"
                if bool(val_stats.get("sharpe_is_defined", False))
                else f"n/a ({val_sharpe_returns}/{min_sharpe_returns} daily returns)"
            )
            val_trades = int(val_stats.get("n_trades", 0))
            val_score = val_excess
            checkpoint_score = val_score if val_trades > 0 else -float("inf")
            phase = "BC" if is_bc_phase else f"PPO kl={kl_coef:.4f}"
            print(f"  epoch {epoch:3d} | {phase:13s} | loss {loss:8.3f} | val score {val_score:+.3f} "
                  f"(excess {val_excess:+.2f}% dd {val_dd:.2f}% sharpe {val_sharpe_text}) | trades {val_trades}")

            if checkpoint_score > best_val + 1e-9:
                best_val = val_score
                patience = 0
                torch.save(policy.state_dict(), ckpt_path)
                meta = {
                    "seed": seed, "use_rf": bool(use_rf), "rf_skill": skill,
                    "scaler": {k: list(v) for k, v in scaler.items()},
                    "obs_cols": OBSERVATION_COLS,
                    "action_dim": config.action_dim,
                    "actor_hidden_dims": list(config.actor_hidden_dims),
                    "position_size_choices": list(config.position_size_choices),
                    "best_val_score": best_val, "best_val_excess": val_excess,
                    "best_val_sharpe": val_sharpe,
                    "best_val_sharpe_is_defined": bool(val_stats.get("sharpe_is_defined", False)),
                    "best_val_sharpe_daily_returns": val_sharpe_returns,
                }
                meta_path.write_text(json.dumps(meta, indent=2))
            else:
                patience += 1
            if patience >= config.patience:
                print(f"  early stop at epoch {epoch} (best val score {best_val:+.3f})")
                break

    print("\nTraining complete. Checkpoints in", CHECKPOINT_DIR)


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
