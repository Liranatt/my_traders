from __future__ import annotations

import asyncio
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import UUID, uuid4

import asyncpg

from database.backtesting.polymarket import PolymarketHistoryClient
from database.backtesting.market_data import YFinanceClient
from database.backtesting.repositories import json_value
from database.backtesting.repositories.calibration import latest_batch_sizes
from database.backtesting.repositories.runs import (
    clone_stored_run_decisions,
    clone_stored_world_state,
    create_historical_run,
    historical_run,
    purge_run,
    record_stage_failure,
    stored_world_source_summary,
    update_run,
)
from database.backtesting.schema import initialize_historical_schema
from database.db_connection import connect
from LLM.gemini_client import GeminiClient
from LLM.ollama_client import OllamaClient
from main_backtesting.config import BacktestConfig, hourly_availability_boundary
from main_backtesting.stages import STAGES, STAGE_FUNCTIONS
from main_backtesting.stages.event_filter import accepted_markets
from main_backtesting.stages.probabilities import detect_passes
from main_backtesting.stages.reports import validate_pipeline_integrity
from strategies.event_driven_long import EventDrivenStrategy

DATABASE_CONNECTION_ATTEMPTS = 5
DATABASE_CONNECTION_RETRY_BASE_SECONDS = 1.0


def _is_database_connection_error(error: BaseException) -> bool:
    if isinstance(error, asyncpg.exceptions.PostgresConnectionError):
        return True
    return (
        isinstance(error, asyncpg.exceptions.InterfaceError)
        and "connection is closed" in str(error).lower()
    )


async def _close_connection(conn: Any | None) -> None:
    if conn is None:
        return
    is_closed = getattr(conn, "is_closed", None)
    if callable(is_closed) and is_closed():
        return
    try:
        await conn.close()
    except Exception as error:
        if not _is_database_connection_error(error):
            raise


class HistoricalBacktestEngine:
    def __init__(
        self,
        config: BacktestConfig,
        *,
        run_id: UUID | None = None,
        stop_after_stage: str | None = None,
        start_at_stage: str | None = None,
        stored_world_source_run_id: UUID | None = None,
        use_llm_clients: bool = True,
    ) -> None:
        self.config = config
        self.run_id = run_id or uuid4()
        if stop_after_stage is not None and stop_after_stage not in STAGES:
            raise ValueError(f"Unknown stop stage: {stop_after_stage}")
        if start_at_stage is not None and start_at_stage not in STAGES:
            raise ValueError(f"Unknown start stage: {start_at_stage}")
        if stored_world_source_run_id is not None and start_at_stage != "prices":
            raise ValueError("Stored-world runs must start at the prices stage")
        if not use_llm_clients and (
            start_at_stage is None or STAGES.index(start_at_stage) <= STAGES.index("asset_worlds")
        ):
            raise ValueError("LLM clients can only be disabled after the asset_worlds stage")
        self.stop_after_stage = stop_after_stage
        self.start_at_stage = start_at_stage
        self.stored_world_source_run_id = stored_world_source_run_id
        self.hourly_boundary = hourly_availability_boundary()
        self.run_dir = self.config.run_dir(self.run_id)
        self.current_work_key: str | None = None
        self.ollama = OllamaClient() if use_llm_clients else None
        self.gemini = GeminiClient() if use_llm_clients else None
        if self.gemini is not None:
            self.gemini.model_name = config.asset_world_model
            self.gemini.thinking_level = config.asset_world_thinking_level
        self.polymarket = PolymarketHistoryClient(chunk_days=config.probability_chunk_days)
        self.prices = YFinanceClient(concurrency=config.price_download_concurrency)
        self.strategy = self._build_strategy()


    def _build_strategy(self) -> EventDrivenStrategy:
        return EventDrivenStrategy(
            trade_notional=self.config.trade_notional,
            range_period=self.config.trailing_range_bars,
            range_multiplier=(
                self.config.trailing_stop_close_volatility_multiplier
                if self.config.asset_price_policy == "daily_close_to_close"
                else self.config.trailing_range_multiplier
            ),
        )


    async def close(self) -> None:
        clients = [self.polymarket]
        if self.ollama is not None:
            clients.append(self.ollama)
        if self.gemini is not None:
            clients.append(self.gemini)
        await asyncio.gather(*[client.close() for client in clients])


    async def _run_stage(
        self,
        conn: Any,
        stage: str,
        function: Callable[[Any, Any], Awaitable[None]],
    ) -> Any:
        last_connection_error: BaseException | None = None
        for attempt in range(1, DATABASE_CONNECTION_ATTEMPTS + 1):
            if conn is None:
                try:
                    conn = await connect()
                    await initialize_historical_schema(conn)
                except Exception as error:
                    await _close_connection(conn)
                    conn = None
                    if not (
                        _is_database_connection_error(error)
                        or isinstance(error, OSError)
                    ):
                        raise
                    last_connection_error = error
                    if attempt < DATABASE_CONNECTION_ATTEMPTS:
                        await self._wait_for_database_retry(stage, attempt, error)
                        continue
                    break

            self.current_work_key = None
            try:
                await update_run(
                    conn,
                    self.run_id,
                    status="running",
                    stage=stage,
                    error=None,
                )
                await function(self, conn)
                return conn
            except Exception as error:
                if not _is_database_connection_error(error):
                    await self._record_stage_failure(stage, error)
                    self._print_stage_failure(stage)
                    raise
                last_connection_error = error
                await _close_connection(conn)
                conn = None
                if attempt < DATABASE_CONNECTION_ATTEMPTS:
                    await self._wait_for_database_retry(stage, attempt, error)
                    continue
                break

        assert last_connection_error is not None
        await self._record_stage_failure(stage, last_connection_error)
        self._print_stage_failure(stage)
        raise last_connection_error


    async def _wait_for_database_retry(
        self,
        stage: str,
        attempt: int,
        error: BaseException,
    ) -> None:
        delay = DATABASE_CONNECTION_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
        print(
            f"[database reconnect] run_id={self.run_id} stage={stage} "
            f"attempt={attempt + 1}/{DATABASE_CONNECTION_ATTEMPTS} "
            f"delay_seconds={delay:g} error={type(error).__name__}: {error}"
        )
        await asyncio.sleep(delay)


    async def _record_stage_failure(
        self,
        stage: str,
        error: BaseException,
    ) -> None:
        failure_conn = None
        try:
            failure_conn = await connect()
            await record_stage_failure(
                failure_conn,
                run_id=self.run_id,
                stage=stage,
                work_key=self.current_work_key,
                error=error,
            )
        except Exception as recording_error:
            print(
                f"[failure record skipped] run_id={self.run_id} stage={stage} "
                f"error={type(recording_error).__name__}: {recording_error}"
            )
        finally:
            await _close_connection(failure_conn)


    def _print_stage_failure(self, stage: str) -> None:
        print(
            f"[failed] run_id={self.run_id} stage={stage} "
            f"work_key={self.current_work_key}"
        )


    async def run(self, *, resume: bool = False) -> UUID:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "graphs" / "trades").mkdir(parents=True, exist_ok=True)
        (self.run_dir / "logs").mkdir(parents=True, exist_ok=True)
        conn = await connect()
        try:
            await initialize_historical_schema(conn)
            source_run_id = getattr(self, "stored_world_source_run_id", None)
            if resume:
                row = await historical_run(conn, self.run_id)
                self.config = BacktestConfig.from_json(json_value(row["config"]))
                self.strategy = self._build_strategy()
                if self.gemini is not None:
                    self.gemini.model_name = self.config.asset_world_model
                    self.gemini.thinking_level = self.config.asset_world_thinking_level
                self.hourly_boundary = row["hourly_boundary"]
                self.run_dir = Path(row["output_dir"])
            else:
                if self.ollama is not None:
                    calibrated = await latest_batch_sizes(conn, self.ollama.model_name)
                    self.config = replace(
                        self.config,
                        event_filter_batch_size=calibrated.get(
                            "event_filter", self.config.event_filter_batch_size
                        ),
                    )
                if source_run_id is not None:
                    source_summary = await stored_world_source_summary(
                        conn,
                        source_run_id,
                    )
                    print(
                        "[stored worlds verified] "
                        f"source_run_id={source_run_id} "
                        f"passes={source_summary['pass_count']} "
                        f"worlds={source_summary['world_count']} "
                        f"assets={source_summary['asset_count']} "
                        f"models={source_summary['model_names']}"
                    )
                await create_historical_run(
                    conn,
                    run_id=self.run_id,
                    config=self.config.to_json(),
                    hourly_boundary=self.hourly_boundary,
                    output_dir=self.run_dir,
                )
                if source_run_id is not None:
                    await clone_stored_run_decisions(
                        conn,
                        source_run_id=source_run_id,
                        target_run_id=self.run_id,
                    )
                    selected_market_ids = [
                        market.market_id for market in await accepted_markets(self, conn)
                    ]
                    clone_summary = await clone_stored_world_state(
                        conn,
                        source_run_id=source_run_id,
                        target_run_id=self.run_id,
                        market_ids=selected_market_ids,
                    )
                    print(
                        "[stored worlds linked] "
                        f"run_id={self.run_id} "
                        f"markets={clone_summary['market_count']} "
                        f"passes={clone_summary['pass_count']} "
                        f"worlds={clone_summary['world_count']}"
                    )
            stage_pairs = list(zip(STAGES, STAGE_FUNCTIONS))
            start_at_stage = getattr(self, "start_at_stage", None)
            if start_at_stage is not None:
                stage_pairs = stage_pairs[STAGES.index(start_at_stage) :]
            for stage, function in stage_pairs:
                conn = await self._run_stage(conn, stage, function)
                if stage == self.stop_after_stage:
                    await update_run(conn, self.run_id, status="paused", stage=stage)
                    return self.run_id
            await update_run(conn, self.run_id, status="complete", stage="complete")
            return self.run_id
        finally:
            await _close_connection(conn)
            await self.close()


BacktestEngine = HistoricalBacktestEngine


async def purge_historical_run(run_id: UUID) -> None:
    conn = await connect()
    try:
        await initialize_historical_schema(conn)
        output_dir = await purge_run(conn, run_id)
    finally:
        await _close_connection(conn)
    if output_dir:
        path = Path(output_dir).resolve()
        root = (Path(__file__).resolve().parent / "output" / "runs").resolve()
        if path.parent != root:
            raise RuntimeError(f"Refusing to purge unexpected run directory: {path}")
        if path.exists():
            shutil.rmtree(path)
