"""Pre-flight preview of the two-pass LLM world builder (no DB writes).

Runs the CURRENT relevance-gate + tight-mapping prompts against a sample of real markets
and reports whether the worlds are tight and related -- earnings collapse to the single
named company, geopolitical/macro markets keep their specific oil/defense/rate names with a
graded `connection_strength`, and the relevance gate drops only genuine non-tradeables.

Use this to sanity-check the fix before a full pipeline rebuild (which is expensive). It does
NOT persist anything; the real rebuild happens via `main_backtesting.main run`.

Usage:
  .venv/Scripts/python.exe -m general_testing.world_rebuild_preview [--per-bucket 4]
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from database.db_connection import connect
from database.backtesting.schema import SCHEMA
from database.backtesting.repositories.worlds import ib_tradable_assets
from main_backtesting.models import SourceMarket
from LLM.build_world import build_gemini_asset_worlds, _single_named_entity_market
from LLM.gemini_client import GeminiClient

BUCKETS = [
    ("earnings", "question ILIKE '%beat quarterly earnings%'"),
    ("military/geo", "(question ILIKE '%Iran%' OR question ILIKE '%strike%' OR question ILIKE '%Israel%')"),
    ("macro/rates", "(question ILIKE '%inflation%' OR question ILIKE '%rate cut%' OR question ILIKE '%Fed %')"),
    ("other", "TRUE"),
]


async def pick_markets(conn, per_bucket: int) -> list[SourceMarket]:
    picked: dict[str, SourceMarket] = {}
    for _label, where in BUCKETS:
        rows = await conn.fetch(f"""
            SELECT DISTINCT ON (question) market_id, event_id, question, created_at, end_at, final_outcome
            FROM {SCHEMA}.historical_run_markets
            WHERE {where}
            ORDER BY question, created_at
            LIMIT {per_bucket}
        """)
        for r in rows:
            if r["market_id"] in picked:
                continue
            picked[r["market_id"]] = SourceMarket(
                market_id=r["market_id"], event_id=r["event_id"],
                event_title=r["question"], question=r["question"],
                created_at=r["created_at"], end_at=r["end_at"], tags=[],
                raw_market={}, yes_token_id="", condition_id=None,
                final_outcome=r["final_outcome"],
            )
    return list(picked.values())


async def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--per-bucket", type=int, default=4)
    args = parser.parse_args()

    conn = await connect()
    try:
        markets = await pick_markets(conn, args.per_bucket)
        catalog = await ib_tradable_assets(conn)
    finally:
        await conn.close()
    print(f"catalog={len(catalog)} ; previewing {len(markets)} real markets\n")

    gemini = GeminiClient()
    requests = [(f"{m.market_id}:1", m, m.created_at) for m in markets]
    try:
        worlds = await build_gemini_asset_worlds(gemini, requests, tradable_assets=catalog)
    finally:
        await gemini.close()

    by_market = {m.market_id: m for m in markets}
    earnings_violations = gate_dropped = cs_missing = cs_total = above_cutoff = 0
    cutoff = 0.50
    for w in sorted(worlds, key=lambda x: -x.question_relevance):
        m = by_market[w.request_id.split(":")[0]]
        single = _single_named_entity_market(m)
        n = len(w.assets)
        gate_dropped += n == 0
        earnings_violations += single and n > 1
        flag = "  !!! EARNINGS>1NAME" if (single and n > 1) else (" <SINGLE-ENTITY>" if single else "")
        print(f"[qrel={w.question_relevance:.2f}] Q: {m.question[:80]}{flag}")
        if n == 0:
            if w.question_relevance < 0.10:
                print("   (dropped at gate: question_relevance below floor)")
            else:
                print("   (no world: tight mapping found no mechanically-exposed US equity)")
        for a in w.assets:
            cs_total += 1
            cs_missing += a.connection_strength is None
            cs = a.connection_strength if a.connection_strength is not None else float("nan")
            final = w.question_relevance * (a.connection_strength or 0.0)
            above_cutoff += final >= cutoff
            mark = "  <== >=0.50" if final >= cutoff else ""
            print(f"   {a.symbol:7} conn={cs:.2f} final={final:.2f} [{a.relationship_type}] {a.reason[:60]}{mark}")
        print()

    print("=" * 60)
    print(f"worlds={len(worlds)} gate_dropped={gate_dropped} earnings>1name={earnings_violations}")
    print(f"connection_strength populated {cs_total - cs_missing}/{cs_total}")
    print(f"picks with final (qrel x conn) >= {cutoff:.2f}: {above_cutoff}/{cs_total}")
    if earnings_violations or cs_missing:
        print("WARNING: earnings peer-dump or missing connection_strength detected.")


if __name__ == "__main__":
    asyncio.run(main())
