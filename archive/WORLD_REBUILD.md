# Task A1 — LLM world rebuild (the "unrelated words" fix)

## What was wrong (verified against the DB, not assumed)

Every world feeding `candidates.parquet` (run `339084de…` and the other 18 `complete` runs)
was built by the **old one-pass "compact" path**, not the two-pass tight mapping. Evidence
pulled from `checking_relevant_events.historical_asset_world_assets`:

- **Earnings = peer dumps.** "Will Domino's beat earnings?" → `DPZ, PZZA, YUM, WEN, MCD, QSR, PECO`
  (7 names). Only `DPZ` mechanically reprices on Domino's own beat; the rest are **unrelated
  words** — peers that inject noise rows, each with their own unrelated `y_hedged` label.
- **Padded to ≥4 names.** The compact path hard-required ≥4 IB symbols, so even single-entity
  earnings were bulked out with irrelevant peers. `avg_world_size = 8.1`, max 11.
- **`connection_strength` was NULL on 10,329 / 10,329 picks** — the contract's `connection_strength`
  feature was dead (defaulted to 1.0 = "unscored").
- **Boilerplate reasons** ("Gemini 3.5 Flash selected X as economically related") — no causal channel.

Effect: ~6 noise rows per earnings event → the alpha model trained on mostly-noise
(`ALPHA_RESULTS.md`: train rank-corr **0.84 → test −0.05**, success **FAIL**).

The two-pass code (relevance gate → tight mapping) had been written but **never ran**: the
DB worlds predated it, and it also **crashed and over-filtered** when invoked (see fixes).

## What changed

| File | Fix |
|---|---|
| `LLM/gemini_client.py` | `structured(..., prefer_prompt_schema=True)` — Gemini returns **HTTP 400** on the nested `TightAssetWorlds` server schema (`enum` + `maxItems` + field `default`). Prompt-schema mode keeps strict Pydantic validation but sidesteps the crash. |
| `LLM/build_world.py` | Both two-pass calls now use `prefer_prompt_schema=True`. **Relevance gate** recalibrated: it was dropping every geopolitical/macro market; it now keeps the proven structural edges (oil-on-supply-threat, rate sensitivity) and drops only true non-tradeables (speech-word, celebrity/sports). **Tight-mapping** prompt strengthened: earnings→1 name (no peers), geopolitical→oil/defense, macro→rate-sensitive, graded `connection_strength`. |
| `main_backtesting/config.py` | `asset_world_prompt_version` → `…v7-gemini-two-pass-tight-mapping`. **Cache-bust**: worlds are content-addressed by `input_hash(prompt_version,…)`, so without this a re-run reuses the stale compact worlds. |
| `general_testing/build_dataset.py` | New `--run-ids` flag. `load_observations` reads **all** `complete` runs and dedupes by `symbol`, so the old peer symbols persist forever. Restrict the dataset to the new run. |
| `testing/test_event_driven_backtest.py` | Replaced the 2 stale compact-path tests with 4 two-pass tests (earnings→1 name, gate-drop→empty, untradeable dropped, all-untradeable hard-crash). |
| `general_testing/world_rebuild_preview.py` | New read-only pre-flight: previews worlds on a sample with no DB writes. |

## Live verification (real markets, real Gemini)

`python -m general_testing.world_rebuild_preview` →
earnings collapse to **1 name** (MMM, ABT, ANF) cs=1.00; ATACMS→`LMT 0.80, ITA 0.70`;
Israel–Iran→`USO 0.80, ITA 0.70`; inflation→`TLT 0.80`; Fed emergency cut→`TLT 0.90, KRE 0.70`.
**0 earnings peer-dumps, 0 gate over-drops, connection_strength populated 13/13.**

## How to re-run the analysis

```bash
# 0. (cheap, no DB writes) sanity-check the worlds first
.venv\Scripts\python.exe -m general_testing.world_rebuild_preview

# 1. Full rebuild — v7 forces a two-pass world rebuild; event_filter/probabilities/prices
#    reuse cache, simulation runs so the run is marked `complete`. Note the printed run_id.
.venv\Scripts\python.exe -m main_backtesting.main run

# 2. Build the dataset from ONLY the new run (avoids old peer-dump contamination)
.venv\Scripts\python.exe -m general_testing.build_dataset --run-ids <NEW_RUN_ID>

# 3. Validate the rebuilt worlds objectively, then re-run the portfolio sweep
.venv\Scripts\python.exe -m general_testing.world_quality_audit <NEW_RUN_ID>
```

`world_quality_audit` should now show: far fewer dead worlds, earnings worlds = 1 name, and
`connection_strength` tracking realized `|y_hedged|`. Then re-check `ALPHA_RESULTS.md`
(success criterion) and the `PORTFOLIO_RESULTS.md` OOS tables on the clean dataset.

Tip: if any world batch fails the all-requests-returned check, lower
`asset_world_batch_size` (config, default 20).
