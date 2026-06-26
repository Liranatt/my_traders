from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from database.backtesting.historical_repository import historical_run
from database.backtesting.repository import candidate_events, event_markets
from database.backtesting.schema import initialize_historical_schema
from database.backtesting.repositories import json_value
from database.db_connection import connect
from main_backtesting.calibration import calibrate_batches
from main_backtesting.config import BacktestConfig
from main_backtesting.engine import STAGES, HistoricalBacktestEngine, purge_historical_run
from main_backtesting.reporting import generate_run_reports


def utc_date(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Expected date in YYYY-MM-DD format") from error


def add_smoke_date_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--start", type=utc_date, default=utc_date("2026-01-01"))
    parser.add_argument(
        "--end",
        type=utc_date,
        default=utc_date("2026-02-01"),
        help="Exclusive end date.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Full resumable historical backtest.")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="Create and execute a new historical backtest.")
    run.add_argument("--max-events", type=int, default=0)

    run_after_worlds = commands.add_parser(
        "run-after-worlds",
        help="Create a new run from stored asset worlds without initializing or calling LLMs.",
    )
    run_after_worlds.add_argument("--source-run-id", type=UUID, required=True)

    candidates = commands.add_parser(
        "list-candidates",
        help="List eligible markets without calling Ollama or downloading market data.",
    )
    add_smoke_date_arguments(candidates)
    candidates.add_argument("--limit", type=int, default=20)

    smoke = commands.add_parser(
        "smoke-test",
        help="Run a bounded backtest for one or more selected events.",
    )
    add_smoke_date_arguments(smoke)
    smoke.add_argument(
        "--event-id",
        action="append",
        default=[],
        help="Exact eligible event ID. Repeat to select multiple events.",
    )
    smoke.add_argument(
        "--max-events",
        type=int,
        default=None,
        help="Maximum events; defaults to one or to the number of selected event IDs.",
    )
    smoke.add_argument(
        "--through",
        choices=STAGES,
        default="event_filter",
        help="Stop safely after this stage. Resume later with the printed run ID.",
    )

    resume = commands.add_parser("resume", help="Resume an interrupted historical backtest.")
    resume.add_argument("--run-id", type=UUID, required=True)
    resume.add_argument(
        "--through",
        choices=STAGES,
        default=None,
        help="Stop safely after this stage instead of completing the entire run.",
    )

    report = commands.add_parser("report", help="Regenerate reports for an existing run.")
    report.add_argument("--run-id", type=UUID, required=True)

    purge = commands.add_parser("purge-run", help="Delete one run without deleting reusable data.")
    purge.add_argument("--run-id", type=UUID, required=True)

    commands.add_parser(
        "calibrate-batches",
        help="Test increasing Ollama batch sizes once and save the largest valid sizes.",
    )
    return parser


async def print_candidates(config: BacktestConfig, limit: int) -> None:
    conn = await connect()
    try:
        events = await candidate_events(
            conn,
            start=config.start,
            end=config.end,
            minimum_days_remaining=config.minimum_days_remaining,
            maximum_days_remaining=config.maximum_days_remaining,
            included_tags=sorted(config.included_tags),
            excluded_tags=sorted(config.excluded_tags),
            limit=limit,
        )
        market_count = 0
        for event in events:
            for market in await event_markets(conn, event):
                duration = (market.end_at - market.created_at).total_seconds() / 86_400
                if not (
                    config.minimum_days_remaining
                    < duration
                    <= config.maximum_days_remaining
                ):
                    continue
                print(
                    f"{market.market_id}\t{event.event_id}\t{market.created_at.date()}\t"
                    f"{market.end_at.date()}\t{market.question}"
                )
                market_count += 1
                if limit and market_count >= limit:
                    break
            if limit and market_count >= limit:
                break
        print(f"[candidate markets] count={market_count}")
    finally:
        await conn.close()


async def regenerate_report(run_id: UUID) -> None:
    conn = await connect()
    try:
        await initialize_historical_schema(conn)
        run = await historical_run(conn, run_id)
        await generate_run_reports(conn, run_id=run_id, run_dir=Path(run["output_dir"]))
    finally:
        await conn.close()


async def saved_run_config(run_id: UUID) -> BacktestConfig:
    conn = await connect()
    try:
        await initialize_historical_schema(conn)
        row = await historical_run(conn, run_id)
        return BacktestConfig.from_json(json_value(row["config"]))
    finally:
        await conn.close()


async def config_for_stored_world_run(source_run_id: UUID) -> BacktestConfig:
    source = await saved_run_config(source_run_id)
    current = BacktestConfig()
    return replace(
        current,
        start=source.start,
        end=source.end,
        maximum_events=source.maximum_events,
        selected_event_ids=source.selected_event_ids,
        threshold=source.threshold,
        minimum_days_remaining=source.minimum_days_remaining,
        maximum_days_remaining=source.maximum_days_remaining,
        historical_data_cutoff=source.historical_data_cutoff,
        included_tags=source.included_tags,
        excluded_tags=source.excluded_tags,
        event_filter_prompt_version=source.event_filter_prompt_version,
        asset_world_model=source.asset_world_model,
        asset_world_thinking_level=source.asset_world_thinking_level,
        asset_world_prompt_version=source.asset_world_prompt_version,
        pipeline_start_stage="prices",
    )


async def main_async() -> None:
    args = build_parser().parse_args()
    config = BacktestConfig()
    if args.command == "calibrate-batches":
        selected = await calibrate_batches(config)
        print(f"[calibration complete] {selected}")
        return
    if args.command == "list-candidates":
        config = replace(config, start=args.start, end=args.end)
        await print_candidates(config, args.limit)
        return
    if args.command == "smoke-test":
        maximum_events = args.max_events
        if maximum_events is None:
            maximum_events = len(args.event_id) if args.event_id else 1
        if maximum_events < 1:
            raise ValueError("Smoke tests require --max-events of at least 1")
        config = replace(
            config,
            start=args.start,
            end=args.end,
            historical_data_cutoff=args.end,
            maximum_events=maximum_events,
            selected_event_ids=tuple(args.event_id),
        )
        engine = HistoricalBacktestEngine(config, stop_after_stage=args.through)
        run_id = await engine.run()
        print(f"[smoke test stopped after {args.through}] run_id={run_id}")
        return
    if args.command == "run":
        config = replace(config, maximum_events=args.max_events)
        engine = HistoricalBacktestEngine(config)
        run_id = await engine.run()
        print(f"[complete] run_id={run_id}")
        return
    if args.command == "run-after-worlds":
        config = await config_for_stored_world_run(args.source_run_id)
        engine = HistoricalBacktestEngine(
            config,
            start_at_stage="prices",
            stored_world_source_run_id=args.source_run_id,
            use_llm_clients=False,
        )
        run_id = await engine.run()
        print(
            f"[complete] run_id={run_id} "
            f"stored_world_source_run_id={args.source_run_id}"
        )
        return
    if args.command == "resume":
        config = await saved_run_config(args.run_id)
        start_at_stage = config.pipeline_start_stage
        use_llm_clients = STAGES.index(start_at_stage) <= STAGES.index("asset_worlds")
        engine = HistoricalBacktestEngine(
            config,
            run_id=args.run_id,
            stop_after_stage=args.through,
            start_at_stage=start_at_stage,
            use_llm_clients=use_llm_clients,
        )
        run_id = await engine.run(resume=True)
        if args.through:
            print(f"[resume stopped after {args.through}] run_id={run_id}")
        else:
            print(f"[complete] run_id={run_id}")
        return
    if args.command == "report":
        await regenerate_report(args.run_id)
        print(f"[report complete] run_id={args.run_id}")
        return
    if args.command == "purge-run":
        await purge_historical_run(args.run_id)
        print(f"[purged] run_id={args.run_id}")


if __name__ == "__main__":
    asyncio.run(main_async())
