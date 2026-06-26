from __future__ import annotations

import json
import time
from uuid import uuid4

from database.backtesting.historical_repository import save_batch_calibration
from database.backtesting.repository import candidate_events, event_markets
from database.backtesting.schema import initialize_historical_schema
from database.db_connection import connect
from LLM.ollama_client import OllamaClient
from LLM.remove_unwanted_markets import classify_markets
from main_backtesting.config import BacktestConfig


async def _test_sizes(task: str, sizes: list[int], function) -> tuple[int, list[dict]]:
    results: list[dict] = []
    selected = 0
    for size in sizes:
        started = time.perf_counter()
        try:
            count, output_size = await function(size)
            results.append(
                {
                    "size": size,
                    "valid": True,
                    "output_count": count,
                    "output_size_bytes": output_size,
                    "duration_seconds": time.perf_counter() - started,
                }
            )
            selected = size
        except Exception as error:
            results.append(
                {
                    "size": size,
                    "valid": False,
                    "error_type": type(error).__name__,
                    "error": str(error),
                    "duration_seconds": time.perf_counter() - started,
                }
            )
            break
    if selected == 0:
        raise RuntimeError(f"{task} calibration failed at batch size 1")
    return selected, results


async def calibrate_batches(config: BacktestConfig) -> dict[str, int]:
    conn = await connect()
    ollama = OllamaClient()
    try:
        await initialize_historical_schema(conn)
        events = await candidate_events(
            conn,
            start=config.start,
            end=config.end,
            minimum_days_remaining=config.minimum_days_remaining,
            maximum_days_remaining=config.maximum_days_remaining,
            included_tags=sorted(config.included_tags),
            excluded_tags=sorted(config.excluded_tags),
            limit=15,
        )
        if not events:
            raise RuntimeError("Batch calibration requires at least one candidate event")

        markets = []
        for event in events:
            markets.extend(await event_markets(conn, event))
        if not markets:
            raise RuntimeError("Batch calibration requires at least one source market")

        async def test_filter(size: int) -> tuple[int, int]:
            result = await classify_markets(ollama, markets[:size])
            output = [item.model_dump(mode="json") for item in result]
            return len(result), len(json.dumps(output, ensure_ascii=False).encode("utf-8"))

        filter_size, filter_results = await _test_sizes(
            "event_filter", [1, 2, 4, 8, 15], test_filter
        )
        await save_batch_calibration(
            conn,
            calibration_id=uuid4(),
            task="event_filter",
            model_name=ollama.model_name,
            tested_sizes=filter_results,
            selected_batch_size=filter_size,
        )

        # Asset-world batches are built by Gemini in a single call per batch, so only
        # the Ollama event-filter batch size is calibrated here.
        return {
            "event_filter": filter_size,
        }
    finally:
        await conn.close()
        await ollama.close()
