"""Per-archetype study: stop pooling regimes. Each archetype gets its OWN split, its OWN model,
and (for earnings/FDA) a fundamental cluster breakdown so the RF can separate mega-cap / low-debt /
high-margin / sector groups; oil-on-military additionally gets the Polymarket score features.

Per-archetype splits (to give the thin structural pockets some OOS, chronological WITHIN archetype):
  earnings : the real chronological split (it already spans train/val/test)
  fda      : 50% train / ~15% val / rest test   (~7 / 2 / 5 of 14)
  oil_mil  : 70% train / 15% val / 15% test

Usage:  .venv/Scripts/python.exe -m general_testing.archetype_study
"""
from __future__ import annotations

import sys
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
from general_testing.build_dataset import fit_tree_model, model_metrics

FUND = ["feat_log_market_cap", "feat_debt_to_equity", "feat_profit_margin", "feat_cash_to_marketcap", "feat_beta"]
PROB = ["feat_prob_at_trigger", "feat_prob_slope_24h", "feat_prob_volatility", "feat_prob_surge_since_t0"]
PRICE = ["feat_runup_since_t0", "feat_asset_2w_trend", "feat_sector_1m_trend", "feat_ytd_change"]
CAT = ["feat_sector"]


def ratio_split(df: pd.DataFrame, r_train: float, r_val: float) -> pd.Series:
    order = df.sort_values("t_theta").index
    n = len(order)
    n_tr, n_va = int(round(n * r_train)), int(round(n * r_val))
    split = pd.Series("test", index=df.index)
    split.loc[order[:n_tr]] = "train"
    split.loc[order[n_tr:n_tr + n_va]] = "val"
    return split


def naive(df: pd.DataFrame, label: str) -> None:
    if len(df) == 0:
        print(f"    {label:28} (n=0)"); return
    raw = df["asset_return"].astype(float)
    print(f"    {label:28} n={len(df):4}  long_hold={raw.mean()*100:+6.2f}%  hit={ (raw>0).mean()*100:4.0f}%  "
          f"med|move|={df['realized_abs_move'].astype(float).median()*100:4.1f}%")


def rf_oos(df: pd.DataFrame, feats_num: list, feats_cat: list, name: str) -> None:
    tr, va, te = df[df.s=="train"], df[df.s=="val"], df[df.s=="test"]
    if len(tr) < 25 or len(va) < 3 or len(te) < 3:
        print(f"  RF {name}: SKIP (tr/va/te={len(tr)}/{len(va)}/{len(te)} too small)"); return
    model, _ = fit_tree_model(tr.assign(split=tr.s), va.assign(split=va.s), feats_num, feats_cat)
    pred = model.predict(df[feats_num + feats_cat])
    score = pd.Series(pred, index=df.index)
    hit = lambda d: float((np.sign(score.loc[d.index]) == np.sign(d["y_hedged"])).mean())
    m = model_metrics(df.assign(split=df.s), pred, "test")
    print(f"  RF {name}: test dir_hit={hit(te)*100:3.0f}%  test rank_corr={m.get('rank_corr',float('nan')):+.3f}  "
          f"train dir_hit={hit(tr)*100:3.0f}% (tr/va/te={len(tr)}/{len(va)}/{len(te)})")


def buckets(df: pd.DataFrame, col: str, name: str, q=3) -> None:
    v = pd.to_numeric(df[col], errors="coerce")
    try:
        tier = pd.qcut(v, q, labels=[f"{name}_lo", f"{name}_mid", f"{name}_hi"], duplicates="drop")
    except Exception:
        return
    for t in tier.cat.categories:
        naive(df[tier == t], str(t))


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    df = pd.read_parquet("data/candidates.parquet")
    df["t_theta"] = pd.to_datetime(df["t_theta"], utc=True, errors="coerce")
    arch = df["feat_archetype"].fillna("")
    groups = {
        "earnings": arch.str.contains("earnings", case=False),
        "fda": arch.str.contains("fda", case=False),
        "oil_military": arch.str.contains("military", case=False) | arch.str.contains("energy", case=False),
        "macro_other": arch.str.contains("inflation|unknown", case=False, regex=True),
    }
    for name, mask in groups.items():
        g = df[mask].copy()
        if name == "earnings":
            g["s"] = g["split"]
        elif name == "fda":
            g["s"] = ratio_split(g, 0.50, 0.15)
        elif name == "oil_military":
            g["s"] = ratio_split(g, 0.70, 0.15)
        else:
            g["s"] = ratio_split(g, 0.60, 0.15)
        counts = g["s"].value_counts().to_dict()
        print(f"\n{'='*92}\n{name.upper()}  n={len(g)}  split={ {k:int(v) for k,v in counts.items()} }")
        for sp in ("train", "val", "test"):
            naive(g[g.s == sp], f"[{sp}] naive long")
        # RF with the right feature block per archetype
        feats = FUND + PRICE + (PROB if name in ("oil_military", "macro_other") else [])
        rf_oos(g, feats, CAT, name)
        if name in ("earnings", "fda"):
            print("  -- fundamental clusters (whole archetype) --")
            buckets(g, "feat_log_market_cap", "cap")
            buckets(g, "feat_debt_to_equity", "debt")
            buckets(g, "feat_profit_margin", "margin")
            print("  -- by sector (top 6) --")
            for sec, sub in sorted(g.groupby("feat_sector"), key=lambda kv: -len(kv[1]))[:6]:
                naive(sub, str(sec)[:26])


if __name__ == "__main__":
    main()
