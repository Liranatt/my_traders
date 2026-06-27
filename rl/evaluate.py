"""
Evaluate frozen RL policies on the terminal holdout (touched only here).

Compares, per seed x benchmark, on the same terminal candidates / dates / cost
model:
  * RL policy,
  * best CEM policy (run_experiments.sim_opp_cost) — the bar to beat,
  * long-only-to-resolution baseline,
  * passive SPY/QQQ (the rotation benchmark; excess vs it = "beat the index").

Success = averaged over seeds, RL mean excess > 0 (beats the index) AND RL mean
Sharpe and mean excess > the CEM policy's, on at least one benchmark and not
worse on the other.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import torch

from rl.config import ACTION_HOLD, RLConfig
from rl.features import attach_static_features, get_rf_predictions
from rl.policy import PolicyNetwork
from rl.ppo import CEM_RECOMMENDS_EXIT_IDX
from rl.sim_with_policy import sim_with_policy
from rl.train import preprocess
from rl.shared import (
    DEFAULT_POLICY,
    daily_equity_sharpe,
    load_paths,
    partition_data,
)

import run_experiments as RE  # for sim_opp_cost (the CEM baseline)

PROJECT = Path(__file__).resolve().parent.parent
RESULT_DIR = PROJECT / "data"
CHECKPOINT_DIR = RESULT_DIR / "rl_experiment_checkpoints"
BRAIN_DUMP_PATH = PROJECT / "rl_brain_dump.log"


def _write_csv(frame: pd.DataFrame, path: Path) -> Path:
    try:
        frame.to_csv(path, index=False)
        return path
    except PermissionError:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        fallback = path.with_name(f"{path.stem}_{stamp}{path.suffix}")
        frame.to_csv(fallback, index=False)
        print(f"  {path.name} is locked; wrote {fallback.name} instead")
        return fallback


def _tag_frame(frame: pd.DataFrame, *, seed: int, benchmark: str, strategy: str) -> pd.DataFrame:
    if frame.empty:
        return frame
    out = frame.copy()
    out.insert(0, "strategy", strategy)
    out.insert(0, "benchmark", benchmark)
    out.insert(0, "seed", seed)
    return out


def load_cem_policy(benchmark: str) -> dict:
    """Best CEM policy for this benchmark from the clean results CSV, else default."""
    csv = RESULT_DIR / "experiment_results_clean.csv"
    fallback = {**DEFAULT_POLICY, "position_size_pct": 0.10, "max_concurrent": 10}
    if not csv.exists():
        return fallback
    try:
        res = pd.read_csv(csv)
        sub = res[res["benchmark"] == benchmark]
        if sub.empty:
            return fallback
        best = sub.loc[sub["oos_excess_return_pct"].astype(float).idxmax()]
        return json.loads(best["policy_json"])
    except Exception:
        return fallback


def _mask_cem_for_policy(obs: torch.Tensor) -> torch.Tensor:
    policy_obs = obs.clone()
    if policy_obs.dim() == 1:
        policy_obs[CEM_RECOMMENDS_EXIT_IDX] = 0.0
    else:
        policy_obs[:, CEM_RECOMMENDS_EXIT_IDX] = 0.0
    return policy_obs


class EvalPolicyAdapter:
    def __init__(self, policy: PolicyNetwork, *, saved_action_dim: int, current_action_dim: int):
        self.policy = policy
        self.saved_action_dim = int(saved_action_dim)
        self.current_action_dim = int(current_action_dim)

    def forward(self, obs: torch.Tensor):
        logits, value = self.policy.forward(_mask_cem_for_policy(obs))
        if self.saved_action_dim == self.current_action_dim:
            return logits, value
        return self._adapt_logits(logits), value

    def _adapt_logits(self, logits: torch.Tensor) -> torch.Tensor:
        # Support old checkpoints when new entry actions are added ahead of EXIT.
        if self.saved_action_dim > self.current_action_dim or self.saved_action_dim < 3:
            raise ValueError(
                f"Unsupported checkpoint action_dim={self.saved_action_dim} for current action_dim={self.current_action_dim}."
            )

        adapted = logits.new_full((logits.shape[0], self.current_action_dim), -1e9)
        old_exit_idx = self.saved_action_dim - 1
        new_exit_idx = self.current_action_dim - 1
        old_enter_count = self.saved_action_dim - 2

        adapted[:, ACTION_HOLD] = logits[:, ACTION_HOLD]
        if old_enter_count > 0:
            adapted[:, 1:1 + old_enter_count] = logits[:, 1:1 + old_enter_count]
        adapted[:, new_exit_idx] = logits[:, old_exit_idx]
        return adapted


async def async_main():
    config = RLConfig()
    if BRAIN_DUMP_PATH.exists():
        BRAIN_DUMP_PATH.unlink()
    df = preprocess(pd.read_parquet(RESULT_DIR / "candidates.parquet"))
    parts = partition_data(df)
    development, preterminal, terminal = parts["development"], parts["preterminal"], parts["terminal"]
    terminal_start = parts["terminal_start"]
    all_preterminal = pd.concat([development, preterminal], axis=0).sort_values("t_theta")
    print(f"terminal holdout size: {len(terminal)}  starts {terminal_start.date()}")

    prices, probs = await load_paths(df)
    rows = []
    trade_logs = []
    equity_logs = []

    for seed in config.outer_seeds:
        ckpt = CHECKPOINT_DIR / f"rl_policy_seed_{seed}.pt"
        meta_path = CHECKPOINT_DIR / f"rl_policy_seed_{seed}.meta.json"
        if not ckpt.exists():
            print(f"seed {seed}: no checkpoint, skipping.")
            continue
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {"use_rf": False, "scaler": {}}
        use_rf, scaler = bool(meta["use_rf"]), meta.get("scaler", {})

        saved_obs_cols = meta.get("obs_cols")
        if saved_obs_cols is not None and len(saved_obs_cols) != config.obs_dim:
            print(
                f"seed {seed}: checkpoint obs_dim={len(saved_obs_cols)}, current obs_dim={config.obs_dim}; "
                "rerun rl.train for sizing-aware policies."
            )
            continue
        saved_action_dim = meta.get("action_dim")
        saved_action_dim = int(saved_action_dim) if saved_action_dim is not None else config.action_dim
        saved_hidden_dims = meta.get("actor_hidden_dims", config.actor_hidden_dims)
        policy = PolicyNetwork(
            obs_dim=config.obs_dim,
            hidden_dims=list(saved_hidden_dims),
            action_dim=saved_action_dim,
        )
        try:
            policy.load_state_dict(torch.load(ckpt, map_location="cpu"))
        except RuntimeError:
            print(f"seed {seed}: checkpoint is incompatible with current policy shape; rerun rl.train.")
            continue
        policy.eval()
        eval_policy = EvalPolicyAdapter(
            policy,
            saved_action_dim=saved_action_dim,
            current_action_dim=config.action_dim,
        )

        term_preds, unit = get_rf_predictions(all_preterminal, terminal, as_of=terminal_start, seed=seed, use_causal_oof=False)
        terminal_feat = attach_static_features(terminal, term_preds, unit, use_rf=use_rf)

        for bench in ("SPY", "QQQ"):
            rl_trades, rl_eq, rl = sim_with_policy(
                terminal_feat, prices, probs, eval_policy, scaler=scaler,
                bench_sym=bench, use_kelly=True, base_ps=config.base_position_size,
                max_concurrent=config.max_concurrent, start_date=terminal_start, end_date=None,
            )
            base_trades, base_eq, base = sim_with_policy(
                terminal_feat, prices, probs, eval_policy, scaler=scaler,
                bench_sym=bench, use_kelly=True, base_ps=config.base_position_size,
                max_concurrent=config.max_concurrent, start_date=terminal_start, end_date=None,
                is_baseline_long_only=True,
            )
            cem_policy = load_cem_policy(bench)
            cem_trades, cem_eq, cem, _ = RE.sim_opp_cost(
                terminal, prices, probs, cem_policy, bench_sym=bench,
                initial=RE.INITIAL_CAPITAL, use_kelly=True,
                start_date=terminal_start, end_date=None,
            )
            cem_sharpe = daily_equity_sharpe(cem_eq) or 0.0
            trade_logs.extend([
                _tag_frame(rl_trades, seed=seed, benchmark=bench, strategy="RL"),
                _tag_frame(base_trades, seed=seed, benchmark=bench, strategy="LongOnly"),
                _tag_frame(cem_trades, seed=seed, benchmark=bench, strategy="CEM"),
            ])
            equity_logs.extend([
                _tag_frame(rl_eq, seed=seed, benchmark=bench, strategy="RL"),
                _tag_frame(base_eq, seed=seed, benchmark=bench, strategy="LongOnly"),
                _tag_frame(cem_eq, seed=seed, benchmark=bench, strategy="CEM"),
            ])

            # Does the agent add anything beyond just entering and holding to
            # resolution? Track RL minus long-only on the same seed/benchmark.
            rl_lo_excess = float(rl["excess_return"]) - float(base["excess_return"])
            rl_lo_sharpe = float(rl["sharpe"]) - float(base["sharpe"])
            for strat, st, sharpe in (
                ("RL", rl, rl["sharpe"]),
                ("CEM", cem, cem_sharpe),
                ("LongOnly", base, base["sharpe"]),
            ):
                rows.append({
                    "seed": seed, "benchmark": bench, "strategy": strat,
                    "total_return": round(float(st["total_return"]), 4),
                    "benchmark_return": round(float(st["benchmark_return"]), 4),
                    "excess_return": round(float(st["excess_return"]), 4),
                    "sharpe": round(float(sharpe), 4),
                    "max_dd": round(float(st["max_dd"]), 4),
                    "n_trades": int(st["n_trades"]),
                    "win_rate": round(float(st.get("win_rate", 0.0)), 2),
                    "avg_position_size": round(float(st.get("avg_position_size", 0.0)), 4),
                    "excess_vs_longonly": round(rl_lo_excess, 4) if strat == "RL" else np.nan,
                    "sharpe_vs_longonly": round(rl_lo_sharpe, 4) if strat == "RL" else np.nan,
                })
            print(f"  seed {seed} {bench}: RL excess {rl['excess_return']:+.2f}% (Sharpe {rl['sharpe']:+.2f}) | "
                  f"CEM excess {cem['excess_return']:+.2f}% (Sharpe {cem_sharpe:+.2f}) | B&H {rl['benchmark_return']:+.2f}% | "
                  f"RL-LongOnly excess {rl_lo_excess:+.2f}% | size {float(rl.get('avg_position_size', 0.0)):.1f}% ")
    res = pd.DataFrame(rows)
    if res.empty:
        print("No results (no checkpoints). Run rl/train.py first.")
        return
    _write_csv(res, RESULT_DIR / "rl_experiment_terminal_holdout.csv")
    non_empty_trades = [frame for frame in trade_logs if not frame.empty]
    if non_empty_trades:
        _write_csv(
            pd.concat(non_empty_trades, ignore_index=True, sort=False),
            RESULT_DIR / "rl_experiment_terminal_trades.csv",
        )
    non_empty_equity = [frame for frame in equity_logs if not frame.empty]
    if non_empty_equity:
        _write_csv(
            pd.concat(non_empty_equity, ignore_index=True, sort=False),
            RESULT_DIR / "rl_experiment_terminal_equity.csv",
        )

    summary = res.groupby(["benchmark", "strategy"]).agg(
        sharpe_mean=("sharpe", "mean"), sharpe_std=("sharpe", "std"),
        excess_mean=("excess_return", "mean"), excess_std=("excess_return", "std"),
        return_mean=("total_return", "mean"), trades_mean=("n_trades", "mean"),
        avg_size_mean=("avg_position_size", "mean"),
    ).reset_index()
    _write_csv(summary, RESULT_DIR / "rl_experiment_summary.csv")

    print("\n=== Terminal holdout summary (mean over seeds) ===")
    print(summary.to_string(index=False))

    rl_only = res[res.strategy == "RL"]
    addon = rl_only.groupby("benchmark").agg(
        rl_minus_lo_excess_mean=("excess_vs_longonly", "mean"),
        rl_minus_lo_excess_std=("excess_vs_longonly", "std"),
        rl_minus_lo_sharpe_mean=("sharpe_vs_longonly", "mean"),
    ).reset_index()
    print("\n=== RL value-add over long-only-to-resolution (mean over seeds) ===")
    print(addon.to_string(index=False))
    _verdict(summary, addon)


def _verdict(summary: pd.DataFrame, addon: pd.DataFrame | None = None) -> None:
    print("\n=== Success check (beat the index AND beat CEM) ===")
    beats_any = False
    not_worse_other = True
    for bench in ("SPY", "QQQ"):
        rl = summary[(summary.benchmark == bench) & (summary.strategy == "RL")]
        cem = summary[(summary.benchmark == bench) & (summary.strategy == "CEM")]
        if rl.empty or cem.empty:
            continue
        rl_e, rl_s = float(rl.excess_mean.iloc[0]), float(rl.sharpe_mean.iloc[0])
        cem_e, cem_s = float(cem.excess_mean.iloc[0]), float(cem.sharpe_mean.iloc[0])
        beat_index = rl_e > 0
        beat_cem = (rl_s > cem_s) and (rl_e > cem_e)
        print(f"  {bench}: RL excess {rl_e:+.2f}% vs index 0 -> {'beats' if beat_index else 'loses'} | "
              f"RL Sharpe {rl_s:+.2f}/excess {rl_e:+.2f}% vs CEM {cem_s:+.2f}/{cem_e:+.2f}% -> {'beats CEM' if beat_cem else 'no'}")
        if addon is not None:
            a = addon[addon.benchmark == bench]
            if not a.empty:
                d = float(a.rl_minus_lo_excess_mean.iloc[0])
                sd = float(a.rl_minus_lo_excess_std.iloc[0]) if pd.notna(a.rl_minus_lo_excess_std.iloc[0]) else 0.0
                tag = "adds value" if d > sd else ("no edge beyond enter-and-hold" if abs(d) <= sd else "worse than enter-and-hold")
                print(f"       vs long-only: excess {d:+.2f}% (±{sd:.2f}) -> {tag}")
        if beat_index and beat_cem:
            beats_any = True
        if not (rl_s >= cem_s - 1e-9):
            not_worse_other = False
    print(f"\n  VERDICT: {'SUCCESS' if (beats_any and not_worse_other) else 'not yet'} "
          f"(needs to beat the index and CEM on >=1 benchmark, not worse on the other)")


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
