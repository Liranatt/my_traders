"""Run Portfolio Layer v1 sweeps and write PORTFOLIO_RESULTS.md."""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import pandas as pd

from .config import DEFAULT_RUN_ID, SimConfig
from .contract import assign_alpha, select
from .data import Candidate, World, load_world
from .simulator import (
    exit_ablation_policies,
    no_toolbox_policy,
    policy_grid,
    score_summary,
    simulate,
)

SPLITS = ("train", "val", "test")
OBJECTIVES = ("sharpe", "calmar", "return_maxdd")
UNIVERSES = ("all", "pre_drop")
DEFAULT_REPORT = Path("PORTFOLIO_RESULTS.md")
DEFAULT_SWEEP_CSV = Path("data/portfolio_sweep_results.csv")


def _fmt_pct(value: float) -> str:
    return f"{value * 100:+.2f}%"


def _fmt_float(value: float) -> str:
    return f"{value:.3f}"


def _split_counts(world: World) -> dict[str, int]:
    counts: dict[str, int] = {}
    for c in world.candidates:
        counts[c.split] = counts.get(c.split, 0) + 1
    return dict(sorted(counts.items()))


def _row(
    *,
    objective: str,
    universe: str,
    split: str,
    policy_id: str,
    policy_label: str,
    result,
    selected: bool = False,
    baseline: str = "",
    maxdd_cap: float = 0.20,
) -> dict:
    s = result.summary
    return {
        "objective": objective,
        "universe": universe,
        "split": split,
        "policy_id": policy_id,
        "policy": policy_label,
        "selected": selected,
        "baseline": baseline,
        "objective_score": score_summary(s, objective, maxdd_cap),
        "n_trades": s["n_trades"],
        "n_days": s["n_days"],
        "active_days": s["active_days"],
        "sharpe": s["sharpe"],
        "maxdd": s["maxdd"],
        "calmar": s["calmar"],
        "ann_return": s["ann_return"],
        "net_pct": s["net_pct"],
        "hit": s["hit"],
        "turnover": s["turnover"],
    }


def _selected_candidates(world: World, split: str, universe: str) -> list[Candidate]:
    return select(world, split, universe)


def _oil_energy_candidates(candidates: Iterable[Candidate]) -> list[Candidate]:
    energy_hedges = {"XLE", "USO", "BNO", "UNG"}
    out: list[Candidate] = []
    for c in candidates:
        text = c.archetype.lower()
        if c.sector_etf in energy_hedges or "oil" in text or "energy" in text:
            out.append(c)
    return out


def run_v1_sweep(
    world: World,
    sim: SimConfig,
    *,
    objectives: Iterable[str],
    universes: Iterable[str],
) -> tuple[list[dict], list[dict]]:
    alpha = assign_alpha(world, sim.alpha_mode, sim.seed)
    all_rows: list[dict] = []
    selected_rows: list[dict] = []
    grid = policy_grid()

    for objective in objectives:
        for universe in universes:
            val_rows: list[dict] = []
            policy_results: dict[str, dict[str, object]] = {}
            for idx, policy in enumerate(grid):
                policy_id = f"p{idx:03d}"
                policy_results[policy_id] = {"policy": policy, "rows": []}
                for split in ("train", "val"):
                    result = simulate(
                        world,
                        _selected_candidates(world, split, universe),
                        alpha,
                        policy,
                        sim,
                    )
                    row = _row(
                        objective=objective,
                        universe=universe,
                        split=split,
                        policy_id=policy_id,
                        policy_label=policy.label(),
                        result=result,
                        maxdd_cap=sim.maxdd_cap,
                    )
                    all_rows.append(row)
                    policy_results[policy_id]["rows"].append(row)
                    if split == "val":
                        val_rows.append(row)

            if val_rows:
                best = max(val_rows, key=lambda r: r["objective_score"])
                selected_id = best["policy_id"]
            else:
                selected_id = "p000"
            selected_policy = policy_results[selected_id]["policy"]
            assert selected_policy is not None

            selected_split_rows = [
                dict(row, selected=True)
                for row in policy_results[selected_id]["rows"]
            ]
            test_result = simulate(
                world,
                _selected_candidates(world, "test", universe),
                alpha,
                selected_policy,
                sim,
            )
            selected_split_rows.append(
                _row(
                    objective=objective,
                    universe=universe,
                    split="test",
                    policy_id=selected_id,
                    policy_label=selected_policy.label(),
                    result=test_result,
                    selected=True,
                    maxdd_cap=sim.maxdd_cap,
                )
            )
            all_rows.extend(selected_split_rows)
            selected_rows.extend(selected_split_rows)
    return all_rows, selected_rows


def run_exit_ablation(world: World, sim: SimConfig, *, universe: str) -> list[dict]:
    """Test exit rules A-E SEPARATELY. Direction = the configured alpha (parquet = the RF);
    the `A_take_profit_rf_magnitude` variant is the RF-direction-+-magnitude experiment."""
    alpha = assign_alpha(world, sim.alpha_mode, sim.seed)
    rows: list[dict] = []
    for rule_name, policy in exit_ablation_policies(entry_strong=0.60):
        for split in ("train", "val", "test"):
            result = simulate(world, _selected_candidates(world, split, universe), alpha, policy, sim)
            row = _row(
                objective=sim.objective, universe=universe, split=split,
                policy_id=rule_name, policy_label=policy.label(), result=result,
                maxdd_cap=sim.maxdd_cap,
            )
            row["rule"] = rule_name
            rows.append(row)
    return rows


def _ablation_table(rows: list[dict]) -> str:
    lines = [
        "| rule | split | trades | sharpe | maxDD | calmar | net | hit | turnover |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            "| {rule} | {split} | {n} | {sharpe} | {maxdd} | {calmar} | {net} | {hit} | {turnover} |".format(
                rule=row["rule"], split=row["split"], n=row["n_trades"],
                sharpe=_fmt_float(row["sharpe"]), maxdd=_fmt_pct(row["maxdd"]),
                calmar=_fmt_float(row["calmar"]), net=_fmt_pct(row["net_pct"]),
                hit=_fmt_pct(row["hit"]), turnover=_fmt_float(row["turnover"]),
            )
        )
    return "\n".join(lines)


def run_baselines(
    world: World,
    sim: SimConfig,
    *,
    universes: Iterable[str],
) -> list[dict]:
    rows: list[dict] = []
    policy = no_toolbox_policy()
    specs = [
        ("equal_weight_alpha_resolution", sim.alpha_mode, False),
        ("long_only_resolution", "long_only", False),
        ("current_ml_feature_resolution", "current_ml", False),
        ("random_alpha_resolution", "random", False),
        ("long_energy_proxy_resolution", "long_only", True),
    ]
    for universe in universes:
        base_candidates = _selected_candidates(world, "test", universe)
        for name, alpha_mode, oil_only in specs:
            candidates = _oil_energy_candidates(base_candidates) if oil_only else base_candidates
            alpha = assign_alpha(world, alpha_mode, sim.seed)
            result = simulate(world, candidates, alpha, policy, sim)
            rows.append(
                _row(
                    objective=sim.objective,
                    universe=universe,
                    split="test",
                    policy_id="baseline",
                    policy_label=policy.label(),
                    result=result,
                    selected=False,
                    baseline=name,
                    maxdd_cap=sim.maxdd_cap,
                )
            )
    return rows


def _selected_table(rows: list[dict]) -> str:
    lines = [
        "| objective | universe | policy | split | trades | sharpe | maxDD | calmar | net | hit | turnover |",
        "|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            "| {objective} | {universe} | {policy} | {split} | {n_trades} | {sharpe} | {maxdd} | {calmar} | {net} | {hit} | {turnover} |".format(
                objective=row["objective"],
                universe=row["universe"],
                policy=row["policy"],
                split=row["split"],
                n_trades=row["n_trades"],
                sharpe=_fmt_float(row["sharpe"]),
                maxdd=_fmt_pct(row["maxdd"]),
                calmar=_fmt_float(row["calmar"]),
                net=_fmt_pct(row["net_pct"]),
                hit=_fmt_pct(row["hit"]),
                turnover=_fmt_float(row["turnover"]),
            )
        )
    return "\n".join(lines)


def _baseline_table(rows: list[dict]) -> str:
    lines = [
        "| universe | baseline | trades | sharpe | maxDD | calmar | net | hit | turnover |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            "| {universe} | {baseline} | {n_trades} | {sharpe} | {maxdd} | {calmar} | {net} | {hit} | {turnover} |".format(
                universe=row["universe"],
                baseline=row["baseline"],
                n_trades=row["n_trades"],
                sharpe=_fmt_float(row["sharpe"]),
                maxdd=_fmt_pct(row["maxdd"]),
                calmar=_fmt_float(row["calmar"]),
                net=_fmt_pct(row["net_pct"]),
                hit=_fmt_pct(row["hit"]),
                turnover=_fmt_float(row["turnover"]),
            )
        )
    return "\n".join(lines)


def _gap_table(rows: list[dict]) -> str:
    by_key: dict[tuple[str, str], dict[str, dict]] = {}
    for row in rows:
        by_key.setdefault((row["objective"], row["universe"]), {})[row["split"]] = row
    lines = [
        "| objective | universe | train_sharpe | test_sharpe | train_minus_test |",
        "|---|---|---:|---:|---:|",
    ]
    for (objective, universe), split_rows in sorted(by_key.items()):
        train = split_rows.get("train", {})
        test = split_rows.get("test", {})
        train_sharpe = float(train.get("sharpe", 0.0))
        test_sharpe = float(test.get("sharpe", 0.0))
        lines.append(
            f"| {objective} | {universe} | {_fmt_float(train_sharpe)} | "
            f"{_fmt_float(test_sharpe)} | {_fmt_float(train_sharpe - test_sharpe)} |"
        )
    return "\n".join(lines)


def write_report(
    path: Path,
    *,
    world: World,
    sim: SimConfig,
    selected_rows: list[dict],
    baseline_rows: list[dict],
    sweep_csv: Path,
) -> None:
    lines = [
        "# Portfolio Layer Results",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "## Dataset",
        "",
        f"- Candidate rows loaded: {len(world.candidates)}",
        f"- Split counts: {_split_counts(world)}",
        f"- Alpha mode: `{sim.alpha_mode}`",
        f"- Constant notional per position: `{sim.book * sim.position_fraction:,.0f}` "
        f"on `{sim.book:,.0f}` book",
        f"- Max positions: {sim.max_positions}; sector cap: {sim.sector_cap}",
        "",
        "The simulator uses the contract candidate table plus raw daily price and probability "
        "series. Positions are sector-hedged and exit dynamically; there is no fixed 10-day "
        "portfolio-layer cap.",
        "",
        "## Selected V1 Policies",
        "",
        "Policy grid: `entry_strong_probability in {0.60,0.65,0.70}` x "
        "`confirmation_window=2d` x `theta_out in {0.50,0.55}` x stop type/level. "
        "Each objective/universe pair is selected on validation; test is evaluated after selection.",
        "",
        _selected_table(selected_rows),
        "",
        "## IS vs OOS Gap",
        "",
        _gap_table(selected_rows),
        "",
        "## OOS Baselines",
        "",
        _baseline_table(baseline_rows),
        "",
        "Notes: `long_energy_proxy_resolution` is the closest available proxy for "
        "`long-oil-on-escalation` using candidate archetype text and energy hedges. "
        "`current_ml_feature_resolution` uses the legacy `feat_ml_dir` direction from "
        "the candidate contract.",
        "",
        "## Artifacts",
        "",
        f"- Full train/val sweep plus selected-policy test rows: `{sweep_csv}`",
        "",
        "## B4 Status",
        "",
        "The learned RL/differentiable policy is intentionally not started. The task gates B4 "
        "behind a v1 floor and a real Model-1 per-trade edge; current `ALPHA_RESULTS.md` marks "
        "Model 1 test success as FAIL, so a learned policy would be overfit-prone frontier work.",
        "",
        "## Config",
        "",
        "```json",
        pd.Series(asdict(sim)).to_json(indent=2),
        "```",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def async_main(args: argparse.Namespace) -> None:
    sim = SimConfig(
        run_id=args.run_id,
        alpha_mode=args.alpha,
        objective=args.objectives[0],
        universe=args.universes[0],
        seed=args.seed,
        book=args.book,
        position_fraction=args.position_fraction,
        max_positions=args.max_positions,
        sector_cap=args.sector_cap,
        maxdd_cap=args.maxdd_cap,
    )
    world = await load_world(args.run_id, args.candidates)

    if args.mode == "exit_ablation":
        ablation_rows: list[dict] = []
        for universe in args.universes:
            ablation_rows.extend(run_exit_ablation(world, sim, universe=universe))
        args.sweep_csv.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(ablation_rows).to_csv(Path("data/exit_ablation_results.csv"), index=False)
        report = Path("EXIT_ABLATION_RESULTS.md")
        lines = [
            "# Exit-Rule Ablation (A-E, tested separately)",
            "",
            f"Generated: {datetime.now(timezone.utc).isoformat()}",
            f"Run `{args.run_id}`; alpha/direction = `{sim.alpha_mode}` (parquet = the RF).",
            "Base = confirmed entry (strong>=0.60) + resolution exit only. Each rule is added alone.",
            "`A_take_profit_rf_magnitude` is the RF-direction-plus-magnitude experiment.",
            "",
        ]
        for universe in args.universes:
            lines += [f"## universe = {universe}", "", _ablation_table([r for r in ablation_rows if r["universe"] == universe]), ""]
        report.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"[done] wrote {report} and data/exit_ablation_results.csv")
        return

    sweep_rows, selected_rows = run_v1_sweep(
        world,
        sim,
        objectives=args.objectives,
        universes=args.universes,
    )
    baseline_rows = run_baselines(world, sim, universes=args.universes)
    args.sweep_csv.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(sweep_rows + baseline_rows).to_csv(args.sweep_csv, index=False)
    write_report(
        args.report,
        world=world,
        sim=sim,
        selected_rows=selected_rows,
        baseline_rows=baseline_rows,
        sweep_csv=args.sweep_csv,
    )
    print(f"[done] wrote {args.report}")
    print(f"[done] wrote {args.sweep_csv}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Portfolio Layer v1 sweeps.")
    parser.add_argument("--run-id", default=DEFAULT_RUN_ID)
    parser.add_argument("--mode", choices=("v1", "exit_ablation"), default="v1",
                        help="v1 = the entry/stop/theta sweep; exit_ablation = test rules A-E separately.")
    parser.add_argument("--candidates", type=Path, default=Path("data/candidates.parquet"))
    parser.add_argument("--alpha", choices=("parquet", "oracle", "random", "long_only", "current_ml"), default="parquet")
    parser.add_argument("--objectives", nargs="+", choices=OBJECTIVES, default=list(OBJECTIVES))
    parser.add_argument("--universes", nargs="+", choices=UNIVERSES, default=list(UNIVERSES))
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--sweep-csv", type=Path, default=DEFAULT_SWEEP_CSV)
    parser.add_argument("--book", type=float, default=100_000.0)
    parser.add_argument("--position-fraction", type=float, default=0.10)
    parser.add_argument("--max-positions", type=int, default=10)
    parser.add_argument("--sector-cap", type=int, default=4)
    parser.add_argument("--maxdd-cap", type=float, default=0.20)
    parser.add_argument("--seed", type=int, default=7)
    return parser.parse_args()


def main() -> None:
    asyncio.run(async_main(parse_args()))


if __name__ == "__main__":
    main()
