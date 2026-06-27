"""
perplexity_finance_batch.py

Async batch runner for Perplexity API finance queries.
- Reads queries from a list (or YAML/JSON file)
- Fires them concurrently in configurable batch sizes
- Exponential backoff on rate-limit errors
- Saves results to a timestamped JSON file
- Uses sonar (cheapest) model by default; swap to sonar-pro for deeper answers
"""

import asyncio
import json
import os
import random
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx

# ── Config ────────────────────────────────────────────────────────────────────

API_KEY   = os.environ["my_traders_api_key_perplexity"]
API_URL   = "https://api.perplexity.ai/chat/completions"
MODEL     = "sonar-pro"               # cheapest; use "sonar-pro" for richer answers
MAX_TOKENS = 512                  # cap output tokens to control cost
BATCH_SIZE = 5                    # concurrent requests per wave
BATCH_DELAY = 1.0                 # seconds between waves (rate-limit buffer)
MAX_RETRIES = 4                   # exponential backoff retries on 429
TIMEOUT    = 30.0                 # per-request timeout (seconds)

# System prompt shared by all finance queries — keeps prompts short = fewer input tokens
SYSTEM_PROMPT = (
    "You are a quantitative financial analyst. Answer factually based on historical context. "
    "You MUST respond ONLY with a valid JSON object. Do not include any markdown formatting or code blocks. "
    "The JSON must have exactly three fields: 'expected_return_pct' (a float, e.g. 5.5), "
    "'confidence_score' (int 1-10), and 'reasoning' (a brief 1-2 sentence explanation). "
    "Do not include any other text."
)

# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class QueryResult:
    query: str
    answer: str = ""
    citations: list[str] = field(default_factory=list)
    model: str = MODEL
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    elapsed_sec: float = 0.0
    error: Optional[str] = None

# ── Token cost estimator (sonar pricing as of June 2026) ─────────────────────
# sonar:     $1 / 1M input,  $1 / 1M output
# sonar-pro: $3 / 1M input,  $15 / 1M output
# Adjust if pricing changes: https://docs.perplexity.ai

COST_TABLE = {
    "sonar":     {"input": 1e-6,  "output": 1e-6},
    "sonar-pro": {"input": 3e-6,  "output": 15e-6},
}

def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    rates = COST_TABLE.get(model, COST_TABLE["sonar"])
    return prompt_tokens * rates["input"] + completion_tokens * rates["output"]

# ── Core async request ─────────────────────────────────────────────────────────

async def query_perplexity(
    client: httpx.AsyncClient,
    query: str,
    retries: int = MAX_RETRIES,
) -> QueryResult:
    result = QueryResult(query=query)
    t0 = time.perf_counter()

    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": query},
        ],
        "max_tokens": MAX_TOKENS,
        "temperature": 0.2,        # lower temp = more factual, fewer tokens wasted
        "return_citations": True,
    }

    for attempt in range(retries + 1):
        try:
            resp = await client.post(
                API_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {API_KEY}",
                    "Content-Type": "application/json",
                },
                timeout=TIMEOUT,
            )

            if resp.status_code == 429:
                if attempt < retries:
                    wait = (2 ** attempt) + random.uniform(0, 1)
                    print(f"  [rate limit] sleeping {wait:.1f}s (attempt {attempt+1}/{retries})")
                    await asyncio.sleep(wait)
                    continue
                result.error = "Rate limit exceeded after retries"
                break

            resp.raise_for_status()
            data = resp.json()

            choice   = data["choices"][0]["message"]
            usage    = data.get("usage", {})
            citations = data.get("citations", [])

            result.answer            = choice["content"].strip()
            result.citations         = citations
            result.prompt_tokens     = usage.get("prompt_tokens", 0)
            result.completion_tokens = usage.get("completion_tokens", 0)
            result.total_tokens      = usage.get("total_tokens", 0)
            result.estimated_cost_usd = estimate_cost(
                MODEL, result.prompt_tokens, result.completion_tokens
            )
            break

        except httpx.TimeoutException:
            result.error = "Timeout"
            break
        except httpx.HTTPStatusError as e:
            result.error = f"HTTP {e.response.status_code}: {e.response.text[:200]}"
            break
        except Exception as e:
            result.error = str(e)
            break

    result.elapsed_sec = round(time.perf_counter() - t0, 2)
    return result

# ── Batch runner ───────────────────────────────────────────────────────────────

async def run_batch(queries: list[str]) -> list[QueryResult]:
    # Connection pooling: keep-alive + limit concurrent connections
    limits = httpx.Limits(
        max_keepalive_connections=20,
        max_connections=50,
        keepalive_expiry=30.0,
    )

    async with httpx.AsyncClient(limits=limits) as client:
        all_results: list[QueryResult] = []
        total_waves = (len(queries) + BATCH_SIZE - 1) // BATCH_SIZE

        for wave_idx in range(0, len(queries), BATCH_SIZE):
            batch = queries[wave_idx : wave_idx + BATCH_SIZE]
            wave_num = wave_idx // BATCH_SIZE + 1
            print(f"\n── Wave {wave_num}/{total_waves} ({len(batch)} queries) ──")

            tasks = [query_perplexity(client, q) for q in batch]
            results = await asyncio.gather(*tasks)

            for r in results:
                label = "✓" if not r.error else "✗"
                print(f"  {label} [{r.elapsed_sec}s | ~${r.estimated_cost_usd:.5f}] {r.query[:60]}")
                if r.error:
                    print(f"    ERROR: {r.error}")

            all_results.extend(results)

            # Pause between waves to stay within rate limits
            if wave_idx + BATCH_SIZE < len(queries):
                await asyncio.sleep(BATCH_DELAY)

    return all_results

# ── Output ─────────────────────────────────────────────────────────────────────

def save_results(results: list[QueryResult], output_dir: str = "results") -> Path:
    Path(output_dir).mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = Path(output_dir) / f"finance_batch_{ts}.json"

    total_cost    = sum(r.estimated_cost_usd for r in results)
    total_tokens  = sum(r.total_tokens for r in results)
    failed        = [r for r in results if r.error]

    summary = {
        "run_at":           ts,
        "model":            MODEL,
        "total_queries":    len(results),
        "successful":       len(results) - len(failed),
        "failed":           len(failed),
        "total_tokens":     total_tokens,
        "estimated_cost_usd": round(total_cost, 6),
        "results":          [asdict(r) for r in results],
    }

    with open(out_path, "w") as f:
        json.dump(summary, f, indent=2)

    print(f"\n{'='*55}")
    print(f"  Done. {len(results)-len(failed)}/{len(results)} succeeded")
    print(f"  Total tokens : {total_tokens:,}")
    print(f"  Est. cost    : ${total_cost:.5f} USD")
    print(f"  Saved to     : {out_path}")
    return out_path

# ── Your finance queries ───────────────────────────────────────────────────────

# Sample Query Built from Actual Candidate Data:
# Question: Will Cintas (CTAS) beat quarterly earnings?
# Symbol: CTAS
# Entry Date (t_theta): 2025-11-26
# Resolution Date (t_e): 2025-12-18

sample_query = (
    "Assume today is November 26, 2025. Do not use or search for any data after this date. "
    "Asset: Cintas (CTAS). "
    "Event: The market is pricing in a >55% probability that Cintas will beat quarterly earnings on December 18, 2025. "
    "If this earnings beat materializes, what is the expected maximum percentage move (peak return) "
    "of CTAS between today (Nov 26) and the earnings resolution (Dec 18)? "
    "Output valid JSON with 'expected_return_pct', 'confidence_score', and 'reasoning'."
)

FINANCE_QUERIES = [
    sample_query
]

# ── Entrypoint ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"Perplexity Finance Batch | model={MODEL} | batch_size={BATCH_SIZE}")
    print(f"Queries: {len(FINANCE_QUERIES)}")

    results = asyncio.run(run_batch(FINANCE_QUERIES))
    save_results(results)