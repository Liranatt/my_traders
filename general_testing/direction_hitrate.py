"""Step 3: how often does the alpha model pick direction right?

Reads the rebuilt candidates.parquet and reports directional hit-rate -- sign(alpha_score) vs the
realized move -- against BOTH the raw asset return and the sector-hedged return, on each split,
and split by the relevance filter and by archetype. Baseline = always-long hit rate.

Usage:
  .venv/Scripts/python.exe -m general_testing.direction_hitrate [--min-relevance 0.5]
"""
from __future__ import annotations

import argparse
import sys

import numpy as np
import pandas as pd


def hit(pred_sign: pd.Series, realized: pd.Series) -> float:
    m = realized != 0
    if m.sum() == 0:
        return float("nan")
    return float((np.sign(pred_sign[m]) == np.sign(realized[m])).mean())


def report(df: pd.DataFrame, label: str) -> None:
    if len(df) == 0:
        print(f"{label:34} (n=0)")
        return
    raw = df["asset_return"].astype(float) if "asset_return" in df else df["y_hedged"].astype(float)
    hed = df["y_hedged"].astype(float)
    score = df["alpha_score"].astype(float)
    long_base = float((raw > 0).mean())
    print(f"{label:34} n={len(df):4}  "
          f"model_hit_raw={hit(score, raw)*100:4.0f}%  model_hit_hedged={hit(score, hed)*100:4.0f}%  "
          f"always_long_raw={long_base*100:4.0f}%  mean_raw={raw.mean()*100:+.2f}%")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--parquet", default="data/candidates.parquet")
    ap.add_argument("--min-relevance", type=float, default=0.5)
    args = ap.parse_args()
    df = pd.read_parquet(args.parquet)
    rel = df["feat_connection_strength"].astype(float) if "feat_connection_strength" in df else pd.Series(1.0, index=df.index)

    for split in ("train", "val", "test"):
        s = df[df["split"].eq(split)]
        print(f"\n=== split={split} ===")
        report(s, "ALL")
        report(s[rel.loc[s.index] >= args.min_relevance], f"relevance>={args.min_relevance}")
        for arch in sorted(s["feat_archetype"].dropna().unique()):
            report(s[s["feat_archetype"].eq(arch)], f"  {arch[:30]}")
    print("\nmodel_hit_* = % of nonzero-move trades where sign(alpha_score) matched the realized move.")
    print("50% = coin flip. always_long_raw = base rate of up moves (the naive long benchmark).")


if __name__ == "__main__":
    main()
