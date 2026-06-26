"""Model 2 — the Portfolio Layer (Agent B).

A day-by-day, sector-hedged portfolio simulator that consumes the `candidates` contract
(`data/candidates.parquet`, produced by Agent A) plus the raw price/probability series from
Postgres, and runs a rule-based {buy, sell, hold} policy with constant position size.

Until Agent A delivers the parquet, `data.load_world` rebuilds the contract scaffold straight
from the DB and `alpha_score` is mocked (see `config.SimConfig.alpha_mode`). The harness, the v1
policy and the sweeps are real; only the alpha signal is a placeholder until Model 1 lands.

Entry point:  ``.venv/Scripts/python.exe -m portfolio_layer.run``
"""
