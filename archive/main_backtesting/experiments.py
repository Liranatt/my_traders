"""Configuration-sweep harness for the historical backtest.

This is intentionally SEPARATE from the live pipeline. Each variant below is just a set
of overrides applied on top of the production ``BacktestConfig``; the engine and stages
are untouched. Run the sweep, read ``comparison.csv``, then -- once a winner is found --
apply it by changing the defaults in ``main_backtesting/config.py``.

Variants run CONCURRENTLY, one OS process each (``--workers`` of them at a time). A
backtest is CPU-bound (sklearn training + the simulation loops are synchronous), so
asyncio would NOT parallelize it -- only separate processes give a real speedup. Each
worker runs its own ``asyncio.run(engine.run())`` with its own DB connections.

Reusable data (Ollama filter decisions, Gemini worlds, yfinance prices, Polymarket
probabilities) is cached in Postgres, so only ml_observations + simulation recompute.

CONCURRENCY SAFETY:
  * The 15 non-threshold variants only READ shared data and write run-scoped tables keyed
    by their own run_id -> fully safe to run in parallel.
  * The two ``threshold_*`` variants change the 55%-crossing timestamps, so they make
    fresh Gemini calls AND insert new asset worlds (no ON CONFLICT on input_hash); two of
    them racing can fail on the unique constraint. Run those with ``--workers 1`` or alone.

Usage:
    .venv\\Scripts\\python.exe -m main_backtesting.experiments --list
    .venv\\Scripts\\python.exe -m main_backtesting.experiments --workers 4
    .venv\\Scripts\\python.exe -m main_backtesting.experiments --only baseline,prob_exit_sustained_0.50_48h
"""
from __future__ import annotations

import argparse
import csv
import json
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from main_backtesting.config import BacktestConfig
from main_backtesting.engine import HistoricalBacktestEngine, purge_historical_run

# variant name -> config field overrides applied on top of the production BacktestConfig.
# Add more variants here -- this is the place to test new ideas without touching the engine.
#
# Production defaults already bank the prior sweep's clean win (query-window features ON).
# This grid hunts the next two levers: (1) an ML stop that is loose enough to stop bleeding
# the +edge out via whipsaw (the 3% fixed stop loses -3999 over 104 stop-outs at 4% win),
# and (2) a replacement for the momentum fallback, whose ROC-reversal exit loses -472 over
# 27 trades while the same names held to resolution made +796.
VARIANTS: dict[str, dict] = {
    # --- references ----------------------------------------------------------------
    # New production defaults (query-window features ON, 3% fixed ML stop, classifier
    # direction, momentum fallback). The reference point for this sweep.
    "baseline": {},
    # Pre-20260623 production (query-window features OFF). Determinism anchor + shows the
    # lift the new default already banked.
    "legacy_no_qwf": {"ml_use_query_window_features": False},

    # --- ML stop loss: "a stop, but not too harsh" (only the ML stop changes) -------
    "ml_stop_5pct": {"ml_stop_loss_pct": 0.05},
    "ml_stop_6pct": {"ml_stop_loss_pct": 0.06},
    # Adaptive: trail on close-to-close volatility instead of a fixed fraction.
    "ml_stop_vol_trail_3x": {"ml_stop_mode": "vol_trail", "ml_vol_stop_multiplier": 3.0},

    # --- fallback rethink: momentum loses; bounded event-window long, or drop it -----
    "fallback_event_window_long": {"fallback_strategy": "event_window_long"},
    "fallback_off": {"fallback_strategy": "none"},

    # --- max-holding-period time stop: make the horizon a control, not an accident ---
    # Median ML hold is ~7d; cap it and see if the front-loaded edge survives a tight
    # horizon (better capital efficiency / % return) or needs room.
    "ml_cap_10d": {"max_holding_days": 10},
    "ml_cap_15d": {"max_holding_days": 15},

    # --- single ML direction factor (carried winner from the prior sweep) -----------
    "regression_dir": {"ml_direction_mode": "regression_sign"},

    # --- stacked best-guess bets ----------------------------------------------------
    # Reproduce the prior sweep's top stack, now on top of the qwf default.
    "stack_winner_5pct": {
        "ml_stop_loss_pct": 0.05,
        "ml_direction_mode": "regression_sign",
        "probability_exit_mode": "immediate_band",
        "probability_exit_band": 0.45,
    },
    # The creative stack: adaptive vol-trailing ML stop + regression direction + immediate
    # oracle exit + bounded event-window-long fallback + 15d holding cap (momentum drag gone).
    "stack_vol_trail_capped": {
        "ml_stop_mode": "vol_trail",
        "ml_vol_stop_multiplier": 3.0,
        "ml_direction_mode": "regression_sign",
        "probability_exit_mode": "immediate_band",
        "probability_exit_band": 0.45,
        "fallback_strategy": "event_window_long",
        "max_holding_days": 15,
    },
    # Same creative stack but a moderate 6% fixed stop instead of vol-trailing -- A/B
    # "adaptive vs simply wider".
    "stack_6pct_capped": {
        "ml_stop_loss_pct": 0.06,
        "ml_direction_mode": "regression_sign",
        "probability_exit_mode": "immediate_band",
        "probability_exit_band": 0.45,
        "fallback_strategy": "event_window_long",
        "max_holding_days": 15,
    },

    # --- act on shorts + hedge every ML trade (sector-neutral idiosyncratic book) -----
    # Decompose: shorts alone, hedge alone, both, and both on top of the best stop. The
    # hedge folds into net_profit, so net/return/win-rate here are the HEDGED P&L.
    "shorts_only": {"enable_shorts": True},
    "hedge_only": {"hedge_ml_trades": True},
    "shorts_hedged": {"enable_shorts": True, "hedge_ml_trades": True},
    "shorts_hedged_vol_trail": {
        "enable_shorts": True,
        "hedge_ml_trades": True,
        "ml_stop_mode": "vol_trail",
    },
}

COLUMN_ORDER = [
    "variant", "overall_net", "overall_return_pct", "ml_net", "ml_return_pct",
    "fallback_net", "counterfactual_long_only_net",
    "return_on_peak_pct", "peak_capital", "starting_capital",
    "classification_accuracy", "overall_trades", "ml_trades", "momentum_trades",
    "overall_win_rate", "ml_win_rate", "overall_profit_factor",
    "elapsed_seconds", "run_id", "error", "overrides",
]


def _utc(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)


async def _run_variant_async(
    name: str, overrides: dict, *, start: datetime, end: datetime,
    max_events: int, keep_runs: bool,
) -> dict:
    config = replace(
        BacktestConfig(),
        start=start,
        end=end,
        maximum_events=max_events,
        historical_data_cutoff=end,
        **overrides,
    )
    engine = HistoricalBacktestEngine(config)
    started = datetime.now(timezone.utc)
    run_id = await engine.run()
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()

    reports = config.run_dir(run_id) / "reports"
    summary = json.loads((reports / "summary.json").read_text(encoding="utf-8"))
    overall = summary.get("overall_performance", {})
    ml = summary.get("strategies", {}).get("machine_learning", {}).get("performance", {})
    momentum = summary.get("strategies", {}).get("momentum", {}).get("performance", {})
    classifier = summary.get("machine_learning", {})
    capital = summary.get("capital", {})
    cf_file = reports / "counterfactual_long_only_ml.json"
    counterfactual = (
        json.loads(cf_file.read_text(encoding="utf-8")) if cf_file.exists() else {}
    )
    overall_net = overall.get("total_net_profit")
    ml_net = ml.get("total_net_profit")
    starting_capital = capital.get("starting_capital") or 0.0
    # Non-ML contribution: momentum OR the hold_to_resolution fallback ("fallback_long"),
    # whichever branch ran. Always defined as overall minus ML.
    fallback_net = (
        overall_net - ml_net
        if overall_net is not None and ml_net is not None
        else None
    )
    row = {
        "variant": name,
        "run_id": str(run_id),
        "elapsed_seconds": round(elapsed, 1),
        "overall_trades": overall.get("trade_count"),
        "overall_net": overall_net,
        "overall_return_pct": capital.get("return_on_starting_capital_pct"),
        "overall_win_rate": overall.get("win_rate"),
        "overall_profit_factor": overall.get("profit_factor"),
        "ml_trades": ml.get("trade_count"),
        "ml_net": ml_net,
        "ml_return_pct": (
            ml_net / starting_capital * 100.0
            if ml_net is not None and starting_capital
            else None
        ),
        "ml_win_rate": ml.get("win_rate"),
        "momentum_trades": momentum.get("trade_count"),
        "momentum_net": momentum.get("total_net_profit"),
        "fallback_net": fallback_net,
        "return_on_peak_pct": capital.get("return_on_peak_capital_pct"),
        "peak_capital": capital.get("peak_capital_deployed"),
        "starting_capital": starting_capital,
        "classification_accuracy": classifier.get("classification_accuracy"),
        "counterfactual_long_only_net": counterfactual.get("total_net_profit"),
        "overrides": json.dumps(overrides),
    }
    if not keep_runs:
        await purge_historical_run(run_id)
    return row


def _run_variant_worker(packed: tuple) -> dict:
    """Process-pool entry point (must be top-level/picklable). Runs one variant end to end
    in its own interpreter and returns the metrics row; never raises."""
    name, overrides, start, end, max_events, keep_runs = packed
    print(f"[sweep] start variant={name} overrides={overrides}", flush=True)
    try:
        return asyncio.run(
            _run_variant_async(
                name, overrides, start=start, end=end,
                max_events=max_events, keep_runs=keep_runs,
            )
        )
    except Exception as error:  # record and keep the rest of the sweep alive
        return {
            "variant": name,
            "run_id": None,
            "error": f"{type(error).__name__}: {error}",
            "overrides": json.dumps(overrides),
        }


def _write_comparison(out_dir: Path, rows: list[dict]) -> None:
    keys = {key for row in rows for key in row}
    columns = [c for c in COLUMN_ORDER if c in keys] + [
        c for c in sorted(keys) if c not in COLUMN_ORDER
    ]
    with (out_dir / "comparison.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    (out_dir / "comparison.json").write_text(
        json.dumps(rows, indent=2, default=str), encoding="utf-8"
    )


def run_sweep(
    *, start: datetime, end: datetime, max_events: int,
    only: list[str] | None, keep_runs: bool, workers: int,
) -> Path:
    selected = {
        name: overrides for name, overrides in VARIANTS.items()
        if not only or name in only
    }
    if not selected:
        raise ValueError(f"No matching variants for --only={only}; see --list")
    out_dir = (
        BacktestConfig().output_root.parent
        / "experiments"
        / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    workers = max(1, min(workers, len(selected)))
    print(
        f"[sweep] {len(selected)} variants across {workers} worker process(es) "
        f"window {start.date()}..{end.date()} -> {out_dir}",
        flush=True,
    )
    rows: list[dict] = []
    started = datetime.now(timezone.utc)
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                _run_variant_worker,
                (name, overrides, start, end, max_events, keep_runs),
            ): name
            for name, overrides in selected.items()
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                row = future.result()
            except Exception as error:  # worker process died hard
                row = {"variant": name, "run_id": None, "error": f"worker_crashed: {error}"}
            rows.append(row)
            done = len(rows)
            return_pct = row.get("overall_return_pct")
            print(
                f"[sweep] done {done}/{len(selected)} variant={name} "
                f"net={row.get('overall_net')} "
                f"return={f'{return_pct:.2f}%' if isinstance(return_pct, (int, float)) else return_pct} "
                f"ml_net={row.get('ml_net')} fallback_net={row.get('fallback_net')} "
                f"acc={row.get('classification_accuracy')} "
                f"cf_long_only={row.get('counterfactual_long_only_net')} "
                f"error={row.get('error')}",
                flush=True,
            )
            _write_comparison(out_dir, rows)  # persist after every completion
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    print(f"[sweep complete] {len(rows)} variants in {elapsed/60:.1f} min -> {out_dir / 'comparison.csv'}")
    return out_dir


def main() -> None:
    base = BacktestConfig()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", type=_utc, default=None, help="Backtest start (default = production config).")
    parser.add_argument("--end", type=_utc, default=None, help="Backtest end (default = production config).")
    parser.add_argument("--max-events", type=int, default=0, help="0 = all events in the window.")
    parser.add_argument("--only", default="", help="Comma-separated variant names (default: all).")
    parser.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 4),
                        help="Concurrent variant processes (default: min(4, CPUs)). Each is a full backtest.")
    parser.add_argument("--keep-runs", action="store_true", help="Keep each variant run in the DB/output (default: purge after reading metrics).")
    parser.add_argument("--list", action="store_true", help="List variants and exit.")
    args = parser.parse_args()
    if args.list:
        for name, overrides in VARIANTS.items():
            print(f"{name}: {overrides}")
        return
    only = [s.strip() for s in args.only.split(",") if s.strip()]
    run_sweep(
        start=args.start or base.start,
        end=args.end or base.historical_data_cutoff,
        max_events=args.max_events,
        only=only or None,
        keep_runs=args.keep_runs,
        workers=args.workers,
    )


if __name__ == "__main__":
    main()
