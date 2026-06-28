"""
Evaluate frozen benchmark-specific RL policies on the terminal holdout.

Success for this iteration is intentionally simple: RL excess_return > 0 on
SPY or QQQ, meaning the policy beats buying and holding that benchmark.
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
from rl.sim_with_policy import sim_with_policy
from rl.train import preprocess
from rl.shared import DEFAULT_POLICY, daily_equity_sharpe, load_paths, partition_data

import run_experiments as RE

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


def _checkpoint_paths(bench_sym: str, seed: int) -> tuple[Path, Path]:
    prefix = f"rl_policy_{bench_sym}_seed_{seed}"
    return CHECKPOINT_DIR / f"{prefix}.pt", CHECKPOINT_DIR / f"{prefix}.meta.json"


def load_cem_policy(benchmark: str) -> dict:
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


class EvalPolicyAdapter:
    def __init__(self, policy: PolicyNetwork, *, saved_action_dim: int, current_action_dim: int):
        self.policy = policy
        self.saved_action_dim = int(saved_action_dim)
        self.current_action_dim = int(current_action_dim)

    def forward(self, obs: torch.Tensor):
        logits, value = self.policy.forward(obs)
        if self.saved_action_dim == self.current_action_dim:
            return logits, value
        return self._adapt_logits(logits), value

    def _adapt_logits(self, logits: torch.Tensor) -> torch.Tensor:
        if self.saved_action_dim > self.current_action_dim or self.saved_action_dim < 3:
            raise ValueError(
                f"Unsupported checkpoint action_dim={self.saved_action_dim} "
                f"for current action_dim={self.current_action_dim}."
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

    for bench in config.benchmarks:
        for seed in config.outer_seeds:
            ckpt, meta_path = _checkpoint_paths(bench, seed)
            if not ckpt.exists():
                print(f"{bench} seed {seed}: no checkpoint, skipping.")
                continue
            meta = json.loads(meta_path.read_text()) if meta_path.exists() else {"use_rf": False, "scaler": {}}
            use_rf, scaler = bool(meta["use_rf"]), meta.get("scaler", {})
            exit_threshold = float(meta.get("exit_threshold", config.default_exit_threshold))

            saved_obs_cols = meta.get("obs_cols")
            if saved_obs_cols is not None and len(saved_obs_cols) != config.obs_dim:
                print(
                    f"{bench} seed {seed}: checkpoint obs_dim={len(saved_obs_cols)}, "
                    f"current obs_dim={config.obs_dim}; rerun rl.train."
                )
                continue
            saved_action_dim = int(meta.get("action_dim", config.action_dim))
            saved_hidden_dims = meta.get("actor_hidden_dims", config.actor_hidden_dims)
            policy = PolicyNetwork(
                obs_dim=config.obs_dim,
                hidden_dims=list(saved_hidden_dims),
                action_dim=saved_action_dim,
            )
            try:
                policy.load_state_dict(torch.load(ckpt, map_location="cpu"))
            except RuntimeError:
                print(f"{bench} seed {seed}: checkpoint incompatible with current policy; rerun rl.train.")
                continue
            policy.eval()
            eval_policy = EvalPolicyAdapter(
                policy,
                saved_action_dim=saved_action_dim,
                current_action_dim=config.action_dim,
            )

            term_preds, unit = get_rf_predictions(
                all_preterminal,
                terminal,
                as_of=terminal_start,
                seed=seed,
                use_causal_oof=False,
            )
            terminal_feat = attach_static_features(terminal, term_preds, unit, use_rf=use_rf)

            rl_trades, rl_eq, rl = sim_with_policy(
                terminal_feat,
                prices,
                probs,
                eval_policy,
                scaler=scaler,
                bench_sym=bench,
                use_kelly=True,
                base_ps=config.base_position_size,
                max_concurrent=config.max_concurrent,
                start_date=terminal_start,
                end_date=None,
                exit_threshold=exit_threshold,
            )
            base_trades, base_eq, base = sim_with_policy(
                terminal_feat,
                prices,
                probs,
                eval_policy,
                scaler=scaler,
                bench_sym=bench,
                use_kelly=True,
                base_ps=config.base_position_size,
                max_concurrent=config.max_concurrent,
                start_date=terminal_start,
                end_date=None,
                is_baseline_long_only=True,
            )
            cem_policy = load_cem_policy(bench)
            cem_trades, cem_eq, cem, _ = RE.sim_opp_cost(
                terminal,
                prices,
                probs,
                cem_policy,
                bench_sym=bench,
                initial=RE.INITIAL_CAPITAL,
                use_kelly=True,
                start_date=terminal_start,
                end_date=None,
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

            rl_lo_excess = float(rl["excess_return"]) - float(base["excess_return"])
            rl_lo_sharpe = float(rl["sharpe"]) - float(base["sharpe"])
            for strat, st, sharpe in (
                ("RL", rl, rl["sharpe"]),
                ("CEM", cem, cem_sharpe),
                ("LongOnly", base, base["sharpe"]),
            ):
                rows.append({
                    "seed": seed,
                    "benchmark": bench,
                    "strategy": strat,
                    "total_return": round(float(st["total_return"]), 4),
                    "benchmark_return": round(float(st["benchmark_return"]), 4),
                    "excess_return": round(float(st["excess_return"]), 4),
                    "sharpe": round(float(sharpe), 4),
                    "max_dd": round(float(st["max_dd"]), 4),
                    "n_trades": int(st["n_trades"]),
                    "win_rate": round(float(st.get("win_rate", 0.0)), 2),
                    "avg_position_size": round(float(st.get("avg_position_size", 0.0)), 4),
                    "exit_threshold": exit_threshold if strat == "RL" else np.nan,
                    "excess_vs_longonly": round(rl_lo_excess, 4) if strat == "RL" else np.nan,
                    "sharpe_vs_longonly": round(rl_lo_sharpe, 4) if strat == "RL" else np.nan,
                })
            print(
                f"  {bench} seed {seed}: RL total {rl['total_return']:+.2f}% "
                f"vs B&H {rl['benchmark_return']:+.2f}% -> excess {rl['excess_return']:+.2f}% "
                f"(exit_th {exit_threshold:.2f}, trades {int(rl['n_trades'])})"
            )

    res = pd.DataFrame(rows)
    if res.empty:
        print("No results (no benchmark-specific checkpoints). Run rl.train first.")
        return
    _write_csv(res, RESULT_DIR / "rl_experiment_terminal_holdout.csv")
    non_empty_trades = [frame for frame in trade_logs if not frame.empty]
    if non_empty_trades:
        _write_csv(pd.concat(non_empty_trades, ignore_index=True, sort=False), RESULT_DIR / "rl_experiment_terminal_trades.csv")
    non_empty_equity = [frame for frame in equity_logs if not frame.empty]
    if non_empty_equity:
        _write_csv(pd.concat(non_empty_equity, ignore_index=True, sort=False), RESULT_DIR / "rl_experiment_terminal_equity.csv")

    summary = res.groupby(["benchmark", "strategy"]).agg(
        sharpe_mean=("sharpe", "mean"),
        sharpe_std=("sharpe", "std"),
        excess_mean=("excess_return", "mean"),
        excess_std=("excess_return", "std"),
        return_mean=("total_return", "mean"),
        trades_mean=("n_trades", "mean"),
        avg_size_mean=("avg_position_size", "mean"),
    ).reset_index()
    _write_csv(summary, RESULT_DIR / "rl_experiment_summary.csv")

    print("\n=== Terminal holdout summary (mean over seeds) ===")
    print(summary.to_string(index=False))
    _verdict(summary)


def _verdict(summary: pd.DataFrame) -> None:
    print("\n=== Success check (beat QQQ or SPY buy-and-hold) ===")
    beats_any = False
    for bench in ("SPY", "QQQ"):
        cem = summary[(summary.benchmark == bench) & (summary.strategy == "CEM")]
        if not cem.empty:
            cem_e = float(cem.excess_mean.iloc[0])
            print(f"  {bench}: CEM excess {cem_e:+.2f}% vs holding -> {'beats' if cem_e > 0 else 'loses'}")

        rl = summary[(summary.benchmark == bench) & (summary.strategy == "RL")]
        if rl.empty:
            continue
        rl_e = float(rl.excess_mean.iloc[0])
        beat_index = rl_e > 0
        beats_any = beats_any or beat_index
        print(f"  {bench}: RL  excess {rl_e:+.2f}% vs holding -> {'beats' if beat_index else 'loses'}")
    print(f"\n  RL VERDICT: {'SUCCESS' if beats_any else 'not yet'}")


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
