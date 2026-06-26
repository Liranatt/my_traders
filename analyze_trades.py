"""Parse backtest output, build CSV, and print deep trade analysis."""
import re, csv, sys
from pathlib import Path
from collections import defaultdict

OUTPUT = Path(r"C:\Users\Liran\AppData\Local\Temp\claude\C--Users-Liran-PycharmProjects-my-traders\d6cbca5b-306b-43dd-ac37-92eba3929943\tasks\bk6dl69vp.output")
CSV_OUT = Path(r"C:\Users\Liran\PycharmProjects\my_traders\data\opp_cost_trades.csv")

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── Parse output file ────────────────────────────────────────────────
text = OUTPUT.read_text(encoding="utf-8", errors="replace")

trade_re = re.compile(
    r"^\s{2}(\w+)\s+(\d{4}-\d{2}-\d{2})\s+(\d{4}-\d{2}-\d{2})\s+"
    r"([+-]\d+\.\d+)%\s+\$\s*([+-]?\d[\d,.]+)\s+\$\s*([\d.]+)\s+(\S+)"
)

section_re = re.compile(r"TRADE DETAILS:\s+(.+)")
current_section = None
trades_by_strategy = defaultdict(list)

for line in text.splitlines():
    m = section_re.search(line)
    if m:
        current_section = m.group(1).strip()
        continue
    if current_section:
        m = trade_re.match(line)
        if m:
            sym, entry, exit_, ret, pnl, txn, reason = m.groups()
            trades_by_strategy[current_section].append({
                "strategy": current_section,
                "symbol": sym,
                "entry_date": entry,
                "exit_date": exit_,
                "return_pct": float(ret),
                "pnl": float(pnl.replace(",", "")),
                "txn_cost": float(txn),
                "exit_reason": reason,
                "holding_days": (
                    (lambda a, b: (
                        __import__("datetime").date.fromisoformat(b) -
                        __import__("datetime").date.fromisoformat(a)
                    ).days)(entry, exit_)
                ),
            })

strategies = list(trades_by_strategy.keys())
print(f"Parsed strategies: {strategies}")
for s, t in trades_by_strategy.items():
    print(f"  {s}: {len(t)} trades")

# ── Write CSV ────────────────────────────────────────────────────────
all_trades = []
for t_list in trades_by_strategy.values():
    all_trades.extend(t_list)

fieldnames = ["strategy","symbol","entry_date","exit_date","return_pct",
              "pnl","txn_cost","exit_reason","holding_days"]
with open(CSV_OUT, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=fieldnames)
    w.writeheader()
    w.writerows(all_trades)
print(f"\nCSV saved to: {CSV_OUT}")

# ── Helper ────────────────────────────────────────────────────────────
def stats(trades):
    if not trades:
        return {}
    rets = [t["return_pct"] for t in trades]
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    total_txn = sum(t["txn_cost"] for t in trades)
    return {
        "n": len(trades),
        "total_pnl": sum(pnls),
        "avg_ret": sum(rets)/len(rets),
        "avg_pnl": sum(pnls)/len(pnls),
        "win_rate": sum(1 for p in pnls if p > 0)/len(pnls)*100,
        "avg_win": sum(wins)/len(wins) if wins else 0,
        "avg_loss": sum(losses)/len(losses) if losses else 0,
        "best": max(pnls),
        "worst": min(pnls),
        "total_txn": total_txn,
        "avg_hold": sum(t["holding_days"] for t in trades)/len(trades),
    }

def is_earnings(t):
    return t["exit_reason"] in ("resolution-1d", "end_of_window") and t["holding_days"] <= 7

# ── Deep analysis: Default vs CEM Sharpe ─────────────────────────────
def_trades  = trades_by_strategy.get("SPY -- Default", [])
cem_trades  = trades_by_strategy.get("SPY -- CEM Sharpe", [])

# Build lookup keys
def key(t):
    return (t["symbol"], t["entry_date"])

def_keys = {key(t): t for t in def_trades}
cem_keys = {key(t): t for t in cem_trades}

kept     = [t for t in cem_trades if key(t) in def_keys]
added    = [t for t in cem_trades if key(t) not in def_keys]   # new entries not in default
dropped  = [t for t in def_trades  if key(t) not in cem_keys]  # default had them, CEM skipped

print("\n" + "="*70)
print("  DEFAULT vs CEM SHARPE — TRADE COMPARISON")
print("="*70)
print(f"\n  Default total trades : {len(def_trades)}")
print(f"  CEM Sharpe trades    : {len(cem_trades)}")
print(f"  Kept same entry      : {len(kept)}")
print(f"  New trades (CEM only): {len(added)}")
print(f"  Dropped by CEM       : {len(dropped)}")

# Stats on dropped trades
drop_s = stats(dropped)
kept_s = stats(kept)
add_s  = stats(added)

print(f"\n  --- Trades CEM DROPPED (stayed in SPY instead) ---")
print(f"  Count: {drop_s['n']}  Win%: {drop_s['win_rate']:.1f}%  "
      f"Avg ret: {drop_s['avg_ret']:+.2f}%  Avg P&L: ${drop_s['avg_pnl']:+.2f}  "
      f"Total P&L: ${drop_s['total_pnl']:+,.0f}")
print(f"  Avg win: ${drop_s['avg_win']:+.0f}  Avg loss: ${drop_s['avg_loss']:+.0f}  "
      f"Best: ${drop_s['best']:+,.0f}  Worst: ${drop_s['worst']:+,.0f}")

# Show the big wins CEM dropped
big_wins_dropped = sorted(dropped, key=lambda t: t["pnl"], reverse=True)[:10]
print(f"\n  Top 10 WINS CEM skipped (left money on table):")
print(f"  {'Symbol':8} {'Entry':12} {'Ret%':>8} {'P&L':>10} {'Reason':20}")
for t in big_wins_dropped:
    if t["pnl"] > 0:
        print(f"  {t['symbol']:8} {t['entry_date']:12} {t['return_pct']:+7.2f}% "
              f"${t['pnl']:>9,.0f}  {t['exit_reason']:20}")

# Show the big losses CEM avoided
big_loss_dropped = sorted(dropped, key=lambda t: t["pnl"])[:10]
print(f"\n  Top 10 LOSSES CEM avoided (saved from disaster):")
print(f"  {'Symbol':8} {'Entry':12} {'Ret%':>8} {'P&L':>10} {'Reason':20}")
for t in big_loss_dropped:
    if t["pnl"] < 0:
        print(f"  {t['symbol']:8} {t['entry_date']:12} {t['return_pct']:+7.2f}% "
              f"${t['pnl']:>9,.0f}  {t['exit_reason']:20}")

print(f"\n  --- Trades CEM KEPT ---")
print(f"  Count: {kept_s['n']}  Win%: {kept_s['win_rate']:.1f}%  "
      f"Avg ret: {kept_s['avg_ret']:+.2f}%  Avg P&L: ${kept_s['avg_pnl']:+.2f}")

print(f"\n  --- New trades CEM ADDED (not in Default) ---")
if add_s:
    print(f"  Count: {add_s['n']}  Win%: {add_s['win_rate']:.1f}%  "
          f"Avg ret: {add_s['avg_ret']:+.2f}%  Avg P&L: ${add_s['avg_pnl']:+.2f}")

# ── Position size comparison ──────────────────────────────────────────
print("\n" + "="*70)
print("  POSITION SIZE ANALYSIS")
print("="*70)
print("\n  Default: 10% of portfolio per trade, max 10 concurrent")
print("  CEM Sharpe: 7.25% of portfolio per trade, max 4 concurrent")
print("\n  At $100k portfolio:")
print("  Default: ~$10,000 per trade, up to $100,000 deployed")
print("  CEM Sharpe: ~$7,250 per trade, up to $29,000 deployed max")
print("  → CEM keeps ~71% of capital in SPY at all times vs ~0% for Default")

# ── Earnings vs Non-earnings breakdown ────────────────────────────────
print("\n" + "="*70)
print("  EARNINGS vs NON-EARNINGS BREAKDOWN (CEM Sharpe)")
print("="*70)

def categorize(trades):
    earn, non_earn = [], []
    for t in trades:
        # Short hold + resolution-1d = likely earnings play
        if t["holding_days"] <= 5:
            earn.append(t)
        else:
            non_earn.append(t)
    return earn, non_earn

earn, non_earn = categorize(cem_trades)
es = stats(earn)
ns = stats(non_earn)

print(f"\n  Short-hold (<=5d, likely earnings): {es['n']} trades")
print(f"    Win%: {es['win_rate']:.1f}%  Avg ret: {es['avg_ret']:+.2f}%  "
      f"Avg P&L: ${es['avg_pnl']:+.2f}  Total P&L: ${es['total_pnl']:+,.0f}")

print(f"\n  Longer-hold (>5d, event/trend): {ns['n']} trades")
print(f"    Win%: {ns['win_rate']:.1f}%  Avg ret: {ns['avg_ret']:+.2f}%  "
      f"Avg P&L: ${ns['avg_pnl']:+.2f}  Total P&L: ${ns['total_pnl']:+,.0f}")

# ── Symbol concentration ───────────────────────────────────────────────
print("\n" + "="*70)
print("  SYMBOL CONCENTRATION (CEM Sharpe — top 10 most traded)")
print("="*70)
sym_count = defaultdict(list)
for t in cem_trades:
    sym_count[t["symbol"]].append(t)
sym_sorted = sorted(sym_count.items(), key=lambda x: len(x[1]), reverse=True)[:12]
print(f"\n  {'Symbol':8} {'Count':>6} {'Win%':>6} {'Total P&L':>12} {'Avg P&L':>10}")
print(f"  {'-'*48}")
for sym, tlist in sym_sorted:
    s = stats(tlist)
    print(f"  {sym:8} {s['n']:6d} {s['win_rate']:5.0f}%  ${s['total_pnl']:>10,.0f}  ${s['avg_pnl']:>8,.0f}")

# ── Exit reason breakdown ──────────────────────────────────────────────
print("\n" + "="*70)
print("  EXIT REASON BREAKDOWN (Default vs CEM Sharpe)")
print("="*70)

def exit_summary(trades, label):
    by_reason = defaultdict(list)
    for t in trades:
        reason = t["exit_reason"].split("<")[0].split("_")[0]
        if "profit" in t["exit_reason"]:
            reason = "profit_lock"
        by_reason[reason].append(t)
    print(f"\n  {label}:")
    for r, tlist in sorted(by_reason.items(), key=lambda x: -len(x[1])):
        s = stats(tlist)
        print(f"    {r:15} {s['n']:4d} trades  win%={s['win_rate']:4.0f}%  "
              f"avg={s['avg_ret']:+5.1f}%  P&L=${s['total_pnl']:+,.0f}")

exit_summary(def_trades,  "Default (197 trades)")
exit_summary(cem_trades,  "CEM Sharpe (131 trades)")

# ── Biggest wins and losses ────────────────────────────────────────────
print("\n" + "="*70)
print("  BIGGEST WINS — CEM Sharpe")
print("="*70)
top_wins = sorted(cem_trades, key=lambda t: t["pnl"], reverse=True)[:10]
print(f"  {'Symbol':8} {'Entry':12} {'Exit':12} {'Ret%':>8} {'P&L':>10} {'Reason'}")
for t in top_wins:
    print(f"  {t['symbol']:8} {t['entry_date']:12} {t['exit_date']:12} "
          f"{t['return_pct']:+7.2f}% ${t['pnl']:>9,.0f}  {t['exit_reason']}")

print("\n" + "="*70)
print("  BIGGEST LOSSES — CEM Sharpe")
print("="*70)
top_losses = sorted(cem_trades, key=lambda t: t["pnl"])[:10]
print(f"  {'Symbol':8} {'Entry':12} {'Exit':12} {'Ret%':>8} {'P&L':>10} {'Reason'}")
for t in top_losses:
    print(f"  {t['symbol']:8} {t['entry_date']:12} {t['exit_date']:12} "
          f"{t['return_pct']:+7.2f}% ${t['pnl']:>9,.0f}  {t['exit_reason']}")

# ── All 4 strategies side by side ─────────────────────────────────────
print("\n" + "="*70)
print("  ALL STRATEGIES — SIDE BY SIDE STATS (SPY base)")
print("="*70)
print(f"\n  {'Metric':25} {'Default':>12} {'CEM Sharpe':>12} {'CEM MaxPnL':>12} {'CEM MinDD':>12}")
print(f"  {'-'*70}")
strat_names = ["SPY -- Default","SPY -- CEM Sharpe","SPY -- CEM Max P&L","SPY -- CEM Min-DD"]
strat_stats = [stats(trades_by_strategy.get(n,[])) for n in strat_names]
rows = [
    ("Trades",         "n",         "%d"),
    ("Win rate",       "win_rate",  "%.1f%%"),
    ("Avg return",     "avg_ret",   "%+.2f%%"),
    ("Avg P&L/trade",  "avg_pnl",   "$%+.0f"),
    ("Avg win",        "avg_win",   "$%+.0f"),
    ("Avg loss",       "avg_loss",  "$%+.0f"),
    ("Best trade",     "best",      "$%+.0f"),
    ("Worst trade",    "worst",     "$%+.0f"),
    ("Total P&L",      "total_pnl", "$%+.0f"),
    ("Total txn cost", "total_txn", "$%.0f"),
    ("Avg hold days",  "avg_hold",  "%.1f"),
]
for label, key_, fmt in rows:
    vals = []
    for s in strat_stats:
        v = s.get(key_, 0)
        vals.append(fmt % v)
    print(f"  {label:25} {vals[0]:>12} {vals[1]:>12} {vals[2]:>12} {vals[3]:>12}")

print(f"\n\nDone. CSV at: {CSV_OUT}")
