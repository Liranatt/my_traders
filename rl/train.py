"""
Leakage-safe PPO training for benchmark-specific long-only RL policies.

The train path uses a weak target-aware teacher for warm-starting and then lets
PPO optimize realized benchmark-relative portfolio rewards. Terminal holdout
remains untouched here; rl.evaluate owns it.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import numpy as np
import pandas as pd
import torch

from rl.config import OBSERVATION_COLS, RLConfig
from rl.env import TradingEnv, has_valid_episode
from rl.features import (
    attach_static_features,
    compute_rf_skill,
    fit_static_scaler,
    get_rf_predictions,
    rf_passes_gate,
)
from rl.policy import PolicyNetwork
from rl.ppo import PPO
from rl.sim_with_policy import sim_with_policy
from rl.teacher import build_teacher_examples
from rl.shared import (
    RELEVANCE_COL,
    load_paths,
    partition_data,
    rows_completed_before,
)

PROJECT = Path(__file__).resolve().parent.parent
CHECKPOINT_DIR = PROJECT / "data" / "rl_experiment_checkpoints"
BRAIN_DUMP_PATH = PROJECT / "rl_brain_dump.log"


def preprocess(df: pd.DataFrame) -> pd.DataFrame:
    df = df[df[RELEVANCE_COL].astype(float) > 0.5].copy()
    df["t_theta"] = pd.to_datetime(df["t_theta"], utc=True)
    df["t_e"] = pd.to_datetime(df["t_e"], utc=True)
    df["label_available_ts"] = pd.to_datetime(df["t_e"], utc=True)
    df = df[df["label_available_ts"].notna()].copy()
    df["_candidate_id"] = df.index.astype(str)
    return df


def build_envs(df_feat, prices, probs, scaler, bench_sym: str) -> list[TradingEnv]:
    envs = []
    for _, row in df_feat.iterrows():
        if has_valid_episode(row, prices, probs, bench_sym):
            envs.append(TradingEnv(row, prices, probs, bench_sym, scaler=scaler))
    return envs


def _checkpoint_paths(bench_sym: str, seed: int) -> tuple[Path, Path]:
    prefix = f"rl_policy_{bench_sym}_seed_{seed}"
    return CHECKPOINT_DIR / f"{prefix}.pt", CHECKPOINT_DIR / f"{prefix}.meta.json"


def _select_exit_threshold(
    val_feat: pd.DataFrame,
    prices: dict,
    probs: dict,
    policy: PolicyNetwork,
    scaler: dict,
    config: RLConfig,
    bench_sym: str,
    start_date,
    end_date,
) -> tuple[float, dict]:
    best_threshold = float(config.default_exit_threshold)
    best_stats: dict | None = None
    best_score = -float("inf")

    for threshold in config.exit_threshold_grid:
        _, _, stats = sim_with_policy(
            val_feat,
            prices,
            probs,
            policy,
            scaler=scaler,
            bench_sym=bench_sym,
            use_kelly=False,
            base_ps=config.base_position_size,
            max_concurrent=config.max_concurrent,
            start_date=start_date,
            end_date=end_date,
            exit_threshold=float(threshold),
        )
        trades = int(stats.get("n_trades", 0))
        score = float(stats.get("excess_return", 0.0)) if trades > 0 else -float("inf")
        if score > best_score:
            best_score = score
            best_threshold = float(threshold)
            best_stats = stats

    if best_stats is None:
        best_stats = {}
    return best_threshold, best_stats


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
    print(
        f"development={len(development)}  "
        f"train(label-complete<{static_start.date()})={len(train_df)}  "
        f"val={len(static_eval)}"
    )

    prices, probs = await load_paths(df)

    for bench_sym in config.benchmarks:
        print(f"\n### benchmark {bench_sym} ###")
        for seed in config.outer_seeds:
            print(f"\n=== {bench_sym} seed {seed} ===")
            torch.manual_seed(seed)
            np.random.seed(seed)
            ckpt_path, meta_path = _checkpoint_paths(bench_sym, seed)
            if ckpt_path.exists():
                ckpt_path.unlink()
            if meta_path.exists():
                meta_path.unlink()

            skill = compute_rf_skill(development, as_of=static_start, seed=seed)
            use_rf = rf_passes_gate(
                skill,
                min_hit_rate=config.min_rf_skill_hit_rate,
                min_rank_ic=config.min_rf_skill_rank_ic,
            )
            print(
                f"RF skill: hit_rate={skill['hit_rate']:.3f} "
                f"rank_ic={skill['rank_ic']:.3f} n={skill['n']} -> use_rf={use_rf}"
            )

            train_preds, unit = get_rf_predictions(
                development,
                train_df,
                as_of=static_start,
                seed=seed,
                use_causal_oof=True,
            )
            train_feat = attach_static_features(train_df, train_preds, unit, use_rf=use_rf)
            scaler = fit_static_scaler(train_feat)
            train_envs = build_envs(train_feat, prices, probs, scaler, bench_sym)
            teacher = build_teacher_examples(train_feat, prices, probs, bench_sym, scaler=scaler)
            print(f"training episodes: {len(train_envs)}  teacher states: {len(teacher)} {teacher.counts}")
            if len(train_envs) < 5:
                print("  too few training episodes; skipping seed.")
                continue

            val_preds, _ = get_rf_predictions(
                development,
                static_eval,
                as_of=static_start,
                seed=seed,
                use_causal_oof=False,
            )
            val_feat = attach_static_features(static_eval, val_preds, unit, use_rf=use_rf)

            policy = PolicyNetwork(
                obs_dim=config.obs_dim,
                hidden_dims=config.actor_hidden_dims,
                action_dim=config.action_dim,
            )
            ppo = PPO(policy, config)

            best_val = -float("inf")
            best_threshold = float(config.default_exit_threshold)
            patience = 0
            for epoch in range(1, config.max_train_epochs + 1):
                is_teacher_phase = epoch <= config.teacher_warmup_epochs and len(teacher) > 0
                if is_teacher_phase:
                    loss = ppo.update_teacher(
                        teacher.obs,
                        teacher.actions,
                        teacher.masks,
                        n_epochs=config.teacher_bc_epochs,
                        batch_size=config.teacher_batch_size,
                    )
                    phase = "TeacherBC"
                else:
                    np.random.shuffle(train_envs)
                    for env in train_envs:
                        env.force_entry = False
                        obs = env.reset()
                        done = False
                        while not done:
                            mask = env.get_action_mask()
                            ot = torch.as_tensor(obs, dtype=torch.float32).unsqueeze(0)
                            mt = torch.as_tensor(mask, dtype=torch.bool).unsqueeze(0)
                            with torch.no_grad():
                                action, log_prob, _, value = policy.get_action(ot, mt)
                            next_obs, reward, done, _ = env.step(int(action.item()))
                            ppo.buffer.obs.append(obs)
                            ppo.buffer.actions.append(int(action.item()))
                            ppo.buffer.logprobs.append(float(log_prob.item()))
                            ppo.buffer.masks.append(mask)
                            ppo.buffer.values.append(float(value.item()))
                            ppo.buffer.rewards.append(float(reward))
                            ppo.buffer.dones.append(bool(done))
                            obs = next_obs
                    progress = max(0.0, min(1.0, (epoch - config.teacher_warmup_epochs - 1) / 15.0))
                    current_entropy = config.entropy_beta * (1.0 - progress)
                    loss = ppo.update(entropy_coef=current_entropy)
                    phase = "PPO"

                policy.eval()
                threshold, val_stats = _select_exit_threshold(
                    val_feat,
                    prices,
                    probs,
                    policy,
                    scaler,
                    config,
                    bench_sym,
                    static_start,
                    static_end,
                )
                policy.train()

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
                checkpoint_score = val_excess if val_trades > 0 else -float("inf")
                print(
                    f"  epoch {epoch:3d} | {phase:9s} | loss {loss:8.3f} | "
                    f"val {val_excess:+.2f}% dd {val_dd:.2f}% "
                    f"sharpe {val_sharpe_text} trades {val_trades} "
                    f"exit_th {threshold:.2f}"
                )

                if checkpoint_score > best_val + 1e-9:
                    best_val = val_excess
                    best_threshold = threshold
                    patience = 0
                    torch.save(policy.state_dict(), ckpt_path)
                    meta = {
                        "seed": seed,
                        "benchmark": bench_sym,
                        "use_rf": bool(use_rf),
                        "rf_skill": skill,
                        "scaler": {k: list(v) for k, v in scaler.items()},
                        "obs_cols": OBSERVATION_COLS,
                        "action_dim": config.action_dim,
                        "actor_hidden_dims": list(config.actor_hidden_dims),
                        "position_size_choices": list(config.position_size_choices),
                        "exit_threshold": best_threshold,
                        "exit_threshold_grid": list(config.exit_threshold_grid),
                        "best_val_score": best_val,
                        "best_val_excess": val_excess,
                        "best_val_sharpe": val_sharpe,
                        "best_val_sharpe_is_defined": bool(val_stats.get("sharpe_is_defined", False)),
                        "best_val_sharpe_daily_returns": val_sharpe_returns,
                        "teacher_counts": teacher.counts,
                    }
                    meta_path.write_text(json.dumps(meta, indent=2))
                else:
                    patience += 1
                if patience >= config.patience:
                    print(f"  early stop at epoch {epoch} (best val score {best_val:+.3f})")
                    break

    print("\nTraining setup complete. Checkpoints will be written in", CHECKPOINT_DIR)


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
