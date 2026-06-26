from __future__ import annotations

import argparse
import csv
import json
import math
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


STRATEGY_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "default": {
        "name": "Default Momentum",
        "description": (
            "The hand-tuned baseline. It enters after a Polymarket probability "
            "breakout and uses a volatility-aware trailing stop."
        ),
        "optimizes": "Simple, auditable execution before model filtering.",
        "parameters": "ATR-style range stop, 55% probability pass, long/short direction from the signal.",
        "use": "Use it as the benchmark for every more complex policy.",
    },
    "momentum": {
        "name": "Polymarket Momentum",
        "description": (
            "A direct event-driven policy that treats a strong Polymarket move "
            "as an early signal for the linked public-market asset."
        ),
        "optimizes": "Fast reaction to the first clean probability threshold crossing.",
        "parameters": "Probability threshold, range period, range multiplier, and trailing stop.",
        "use": "Best for measuring whether the event-to-asset mapping has raw edge.",
    },
    "cem": {
        "name": "CEM",
        "description": (
            "Cross-Entropy Method search over policy knobs. It keeps the better "
            "parameter samples and repeatedly resamples around them."
        ),
        "optimizes": "Mean return across the calibration sample.",
        "parameters": "Entry threshold, stop width, lock threshold, hold windows, and sizing knobs.",
        "use": "Use when the objective is raw return and the sample size is large enough.",
    },
    "cem_sharpe": {
        "name": "CEM Sharpe",
        "description": (
            "The same evolutionary search, but the scoring function favors "
            "risk-adjusted return instead of raw profit."
        ),
        "optimizes": "Sharpe ratio and smoother equity behavior.",
        "parameters": "The same policy knobs as CEM, selected for return per unit of volatility.",
        "use": "Use when drawdowns matter as much as total profit.",
    },
    "rf": {
        "name": "Random Forest Filter",
        "description": (
            "A machine-learning filter that predicts whether a candidate trade "
            "is likely to be profitable before allowing it through."
        ),
        "optimizes": "Trade selection quality: keep predicted winners, reject weak candidates.",
        "parameters": "Lean event/asset features, predicted return, and positive-return gate.",
        "use": "Use as a filter layered on top of an explainable trading policy.",
    },
    "default_rf": {
        "name": "Default + RF",
        "description": "The default baseline only trades when the RF filter agrees.",
        "optimizes": "Lower false-positive rate while keeping the baseline execution rules.",
        "parameters": "Default policy parameters plus RF predicted-return gate.",
        "use": "Use when interpretability and model discipline both matter.",
    },
    "cem_sharpe_rf": {
        "name": "CEM Sharpe + RF",
        "description": "Risk-aware CEM parameters with an RF quality gate.",
        "optimizes": "A balance of smoother portfolio behavior and better trade selection.",
        "parameters": "CEM Sharpe policy parameters plus RF predicted-return gate.",
        "use": "Use as the premium policy when validation supports the extra complexity.",
    },
}


RISK_EXPLANATIONS: list[dict[str, str]] = [
    {
        "metric": "Sharpe Ratio",
        "meaning": "Annualized return per unit of total volatility.",
        "good": "> 2 strong, 1-2 acceptable, < 1 weak.",
    },
    {
        "metric": "Sortino Ratio",
        "meaning": "Like Sharpe, but it penalizes only downside volatility.",
        "good": "> 2 strong, 1-2 acceptable, < 1 weak.",
    },
    {
        "metric": "Max Drawdown",
        "meaning": "Largest peak-to-trough loss in the realized equity curve.",
        "good": "Near 0 is better; below -30% needs attention.",
    },
    {
        "metric": "Calmar Ratio",
        "meaning": "Annualized return divided by absolute max drawdown.",
        "good": "> 1 is good; > 3 is excellent.",
    },
    {
        "metric": "VaR 95%",
        "meaning": "The loss threshold exceeded by the worst 5% of daily outcomes.",
        "good": "Less negative is better.",
    },
    {
        "metric": "CVaR 95%",
        "meaning": "Average loss inside the worst 5% of daily outcomes.",
        "good": "Less negative is better; this is stricter than VaR.",
    },
    {
        "metric": "Profit Factor",
        "meaning": "Gross gains divided by gross losses.",
        "good": "> 1 profitable, > 1.5 healthy, > 2 strong.",
    },
]


def _read_csv(path: Path) -> list[dict[str, Any]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        return [_coerce_row(row) for row in csv.DictReader(handle)]


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists() or path.stat().st_size == 0:
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def _coerce_row(row: dict[str, Any]) -> dict[str, Any]:
    return {key: _coerce_value(value) for key, value in row.items()}


def _coerce_value(value: Any) -> Any:
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    text = value.strip()
    if text == "":
        return None
    if text.lower() == "true":
        return True
    if text.lower() == "false":
        return False
    if text.lower() in {"none", "null"}:
        return None
    if (text.startswith("{") and text.endswith("}")) or (
        text.startswith("[") and text.endswith("]")
    ):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return value
    try:
        if re.fullmatch(r"[-+]?\d+", text):
            return int(text)
        if re.fullmatch(r"[-+]?(?:\d+\.\d*|\d*\.\d+)(?:[eE][-+]?\d+)?", text):
            return float(text)
    except ValueError:
        return value
    return value


def _float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(number) or math.isinf(number):
        return default
    return number


def _date_part(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    if len(text) >= 10:
        return text[:10]
    return text or None


def _parse_date(value: Any) -> date | None:
    text = _date_part(value)
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def _strategy_key(value: Any) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", str(value or "default").lower()).strip("_")
    if key in STRATEGY_DESCRIPTIONS:
        return key
    if "sharpe" in key and "rf" in key:
        return "cem_sharpe_rf"
    if "default" in key and "rf" in key:
        return "default_rf"
    if "random" in key or key == "rf" or "machine_learning" in key:
        return "rf"
    if "sharpe" in key:
        return "cem_sharpe"
    if "cem" in key:
        return "cem"
    if "momentum" in key or "poly" in key:
        return "momentum"
    return "default"


def _performance_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    profits = [_float(row.get("net_profit")) for row in rows]
    wins = [value for value in profits if value > 0]
    losses = [value for value in profits if value < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "trade_count": len(rows),
        "total_net_profit": sum(profits),
        "win_rate": len(wins) / len(profits) if profits else None,
        "average_winner": sum(wins) / len(wins) if wins else None,
        "average_loser": sum(losses) / len(losses) if losses else None,
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "expectancy": sum(profits) / len(profits) if profits else None,
    }


def _equity_rows(trades: list[dict[str, Any]], starting_capital: float = 100_000.0) -> list[dict[str, Any]]:
    daily_pnl: dict[date, float] = defaultdict(float)
    for trade in trades:
        day = _parse_date(trade.get("exit_at") or trade.get("entry_at"))
        if day is not None:
            daily_pnl[day] += _float(trade.get("net_profit"))
    if not daily_pnl:
        return []
    cursor = min(daily_pnl)
    final_day = max(daily_pnl)
    equity = peak = starting_capital
    rows: list[dict[str, Any]] = []
    while cursor <= final_day:
        pnl = daily_pnl.get(cursor, 0.0)
        equity += pnl
        peak = max(peak, equity)
        rows.append(
            {
                "date": cursor.isoformat(),
                "pnl": pnl,
                "equity": equity,
                "drawdown": equity - peak,
                "drawdown_pct": (equity - peak) / peak if peak else 0,
            }
        )
        cursor += timedelta(days=1)
    return rows


def _risk_metrics(name: str, trades: list[dict[str, Any]]) -> dict[str, Any]:
    equity = _equity_rows(trades)
    returns = [
        row["pnl"] / 100_000.0
        for row in equity
        if row.get("pnl") is not None
    ]
    mean_return = sum(returns) / len(returns) if returns else 0.0
    variance = (
        sum((value - mean_return) ** 2 for value in returns) / len(returns)
        if returns
        else 0.0
    )
    downside = [min(value, 0.0) for value in returns]
    downside_variance = (
        sum(value * value for value in downside) / len(downside) if downside else 0.0
    )
    sharpe = mean_return / math.sqrt(variance) * math.sqrt(252) if variance > 0 else None
    sortino = (
        mean_return / math.sqrt(downside_variance) * math.sqrt(252)
        if downside_variance > 0
        else None
    )
    max_dd_pct = min((row["drawdown_pct"] for row in equity), default=0.0)
    total_return = (equity[-1]["equity"] / 100_000.0 - 1.0) if equity else 0.0
    years = max(len(equity) / 365.25, 1 / 365.25) if equity else 1
    annual_return = (1 + total_return) ** (1 / years) - 1 if total_return > -1 else -1
    sorted_returns = sorted(returns)
    var_95 = sorted_returns[max(0, int(len(sorted_returns) * 0.05) - 1)] if returns else None
    tail = sorted_returns[: max(1, int(len(sorted_returns) * 0.05))] if returns else []
    cvar_95 = sum(tail) / len(tail) if tail else None
    perf = _performance_summary(trades)
    return {
        "strategy": name,
        "trade_count": len(trades),
        "total_return": total_return,
        "annual_return": annual_return,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": max_dd_pct,
        "calmar": annual_return / abs(max_dd_pct) if max_dd_pct else None,
        "var_95": var_95,
        "cvar_95": cvar_95,
        "profit_factor": perf.get("profit_factor"),
        "win_rate": perf.get("win_rate"),
    }


def _group_by(rows: list[dict[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get(key) or "unknown")].append(row)
    return dict(grouped)


def _find_experiment_log(report_dir: Path, project_root: Path | None) -> Path | None:
    candidates = [
        report_dir / "EXPERIMENTS_LOG.md",
        report_dir.parent / "EXPERIMENTS_LOG.md",
    ]
    if project_root is not None:
        candidates.extend(
            [
                project_root / "EXPERIMENTS_LOG.md",
                project_root / "docs" / "EXPERIMENTS_LOG.md",
                project_root / "main_backtesting" / "EXPERIMENTS_LOG.md",
            ]
        )
    for path in candidates:
        if path.exists():
            return path
    return None


def _parse_experiment_log(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {
            "source": None,
            "phases": [],
            "missing": (
                "EXPERIMENTS_LOG.md was not found. Drop the log next to the report "
                "folder or at the project root and regenerate the report."
            ),
        }
    text = path.read_text(encoding="utf-8", errors="replace")
    phases: list[dict[str, Any]] = []
    current_phase: dict[str, Any] | None = None
    current_item: dict[str, Any] | None = None

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if heading:
            level = len(heading.group(1))
            title = heading.group(2).strip()
            if level <= 2:
                current_phase = {"title": title, "items": [], "body": []}
                phases.append(current_phase)
                current_item = None
            else:
                if current_phase is None:
                    current_phase = {"title": "Experiments", "items": [], "body": []}
                    phases.append(current_phase)
                current_item = {"title": title, "details": []}
                current_phase["items"].append(current_item)
            continue
        bullet = re.match(r"^\s*[-*]\s+(.+)$", line)
        if bullet:
            if current_phase is None:
                current_phase = {"title": "Experiments", "items": [], "body": []}
                phases.append(current_phase)
            if current_item is None:
                current_item = {"title": bullet.group(1).strip(), "details": []}
                current_phase["items"].append(current_item)
            else:
                current_item["details"].append(bullet.group(1).strip())
            continue
        if line.strip():
            if current_item is not None:
                current_item["details"].append(line.strip())
            elif current_phase is not None:
                current_phase["body"].append(line.strip())

    if not phases:
        phases = [{"title": "Experiment Log", "body": [text[:2000]], "items": []}]
    return {"source": str(path), "phases": phases, "missing": None}


def _project_root_from_report_dir(report_dir: Path) -> Path | None:
    for parent in [report_dir, *report_dir.parents]:
        if (parent / ".git").exists():
            return parent
    return None


def build_dashboard_payload(
    report_dir: Path,
    *,
    experiment_log_path: Path | None = None,
    project_root: Path | None = None,
) -> dict[str, Any]:
    report_dir = report_dir.resolve()
    project_root = project_root or _project_root_from_report_dir(report_dir)
    trades = _read_csv(report_dir / "trade_enriched.csv") or _read_csv(report_dir / "trades.csv")
    trades.sort(key=lambda row: str(row.get("entry_at") or ""))
    summary = _read_json(report_dir / "summary.json", {})
    ml_metrics = _read_json(report_dir / "machine_learning_metrics.json", {})
    funnel = _read_json(report_dir / "event_processing_funnel.json", {})
    daily_portfolio = _read_csv(report_dir / "daily_realized_portfolio.csv")
    if daily_portfolio:
        equity = []
        for row in daily_portfolio:
            equity.append(
                {
                    "date": _date_part(row.get("date")),
                    "pnl": _float(row.get("realized_daily_pnl")),
                    "equity": 100_000.0 + _float(row.get("cumulative_pnl")),
                    "drawdown": _float(row.get("drawdown")),
                    "drawdown_pct": _float(row.get("drawdown")) / 100_000.0,
                }
            )
    else:
        equity = _equity_rows(trades)

    worlds = _read_csv(report_dir / "asset_worlds.csv")
    observations = _read_csv(report_dir / "ml_observations.csv")
    predictions = _read_csv(report_dir / "ml_predictions.csv")
    snapshots = _read_csv(report_dir / "ml_model_snapshots.csv")
    benchmark_daily = _read_csv(report_dir / "benchmark_daily.csv")

    world_by_key: dict[str, dict[str, Any]] = {}
    for world in worlds:
        key = "|".join(
            [
                str(world.get("market_id") or ""),
                str(world.get("pass_number") or ""),
                str(world.get("symbol") or ""),
            ]
        )
        world_by_key[key] = world

    obs_by_key: dict[str, dict[str, Any]] = {}
    for obs in observations:
        key = "|".join([str(obs.get("event_id") or ""), str(obs.get("symbol") or "")])
        obs_by_key[key] = obs

    for trade in trades:
        world_key = "|".join(
            [
                str(trade.get("market_id") or ""),
                str(trade.get("pass_number") or ""),
                str(trade.get("symbol") or ""),
            ]
        )
        obs_key = "|".join([str(trade.get("event_id") or ""), str(trade.get("symbol") or "")])
        world = world_by_key.get(world_key, {})
        obs = obs_by_key.get(obs_key, {})
        trade.setdefault("event_archetype", obs.get("event_archetype") or "not_ml_eligible")
        trade["world_universe"] = world.get("universe_name")
        trade["world_reason"] = world.get("universe_reason") or world.get("asset_reason")
        trade["asset_reason"] = world.get("asset_reason")
        features = obs.get("features") if isinstance(obs.get("features"), dict) else {}
        trade["relevance_score"] = features.get("feat_connection_strength") if features else None

    trades_by_strategy = _group_by(trades, "strategy_branch")
    strategy_summaries = [
        {"strategy": name, **_performance_summary(rows)}
        for name, rows in sorted(trades_by_strategy.items())
    ]
    risk_rows = [_risk_metrics("All Strategies", trades)] + [
        _risk_metrics(name, rows) for name, rows in sorted(trades_by_strategy.items())
    ]

    archetype_rows = []
    for name, rows in sorted(_group_by(trades, "event_archetype").items()):
        perf = _performance_summary(rows)
        sample = rows[0] if rows else {}
        archetype_rows.append(
            {
                "event_archetype": name,
                **perf,
                "sample_question": sample.get("question"),
                "sample_world": sample.get("world_universe"),
                "sample_reason": sample.get("world_reason") or sample.get("asset_reason"),
                "sample_symbol": sample.get("symbol"),
                "sample_relevance": sample.get("relevance_score"),
            }
        )

    trade_paths: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in _read_csv(report_dir / "trade_price_paths.csv"):
        trade_id = str(row.get("trade_id") or "")
        if trade_id:
            trade_paths[trade_id].append(
                {
                    "timestamp": row.get("timestamp"),
                    "symbol": row.get("path_symbol") or row.get("symbol"),
                    "close": _float(row.get("close")),
                }
            )

    probability_paths: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in _read_csv(report_dir / "trade_probability_paths.csv"):
        trade_id = str(row.get("trade_id") or "")
        if trade_id:
            probability_paths[trade_id].append(
                {
                    "timestamp": row.get("timestamp"),
                    "probability": _float(row.get("probability")),
                }
            )

    experiment_log_path = experiment_log_path or _find_experiment_log(report_dir, project_root)

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "report_dir": str(report_dir),
        "summary": summary,
        "funnel": funnel,
        "trades": trades,
        "strategy_summaries": strategy_summaries,
        "risk_rows": risk_rows,
        "archetype_rows": archetype_rows,
        "equity": equity,
        "benchmark_daily": benchmark_daily,
        "predictions": predictions,
        "snapshots": snapshots,
        "ml_metrics": ml_metrics,
        "trade_paths": trade_paths,
        "probability_paths": probability_paths,
        "experiments": _parse_experiment_log(experiment_log_path),
        "strategy_descriptions": STRATEGY_DESCRIPTIONS,
        "risk_explanations": RISK_EXPLANATIONS,
    }


def build_html(payload: dict[str, Any]) -> str:
    data_json = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).replace("<", "\\u003c")
    return (
        HTML_TEMPLATE.replace("__DASHBOARD_DATA__", data_json)
        .replace("__PLOTLY_SCRIPT__", _plotly_script_tag())
    )


def _plotly_script_tag() -> str:
    try:
        from plotly.offline.offline import get_plotlyjs

        plotly_js = get_plotlyjs().replace("</script", "<\\/script")
        return f"<script>{plotly_js}</script>"
    except Exception:
        return '<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>'


def write_dashboard(
    report_dir: Path,
    *,
    output_path: Path | None = None,
    experiment_log_path: Path | None = None,
    project_root: Path | None = None,
) -> Path:
    report_dir = report_dir.resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_path or report_dir / "dashboard.html"
    payload = build_dashboard_payload(
        report_dir,
        experiment_log_path=experiment_log_path,
        project_root=project_root,
    )
    output_path.write_text(build_html(payload), encoding="utf-8")
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the premium backtest dashboard.")
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--experiments-log", type=Path, default=None)
    args = parser.parse_args()
    path = write_dashboard(
        args.report_dir,
        output_path=args.output,
        experiment_log_path=args.experiments_log,
    )
    print(f"[dashboard complete] {path}")


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Polymarket Event Trading Dashboard</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  __PLOTLY_SCRIPT__
  <style>
    :root {
      color-scheme: dark;
      --bg: #091016;
      --panel: rgba(15, 23, 32, 0.76);
      --panel-strong: rgba(18, 29, 40, 0.94);
      --line: rgba(149, 164, 182, 0.22);
      --text: #edf4fb;
      --muted: #a9b8c7;
      --blue: #5fb3ff;
      --green: #54d18a;
      --red: #ff6b6b;
      --amber: #ffd166;
      --violet: #b794ff;
      --cyan: #46d9d0;
      --shadow: 0 24px 80px rgba(0, 0, 0, 0.36);
    }
    * { box-sizing: border-box; }
    html { scroll-behavior: smooth; }
    body {
      margin: 0;
      font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background:
        radial-gradient(circle at 16% 8%, rgba(95, 179, 255, 0.16), transparent 26rem),
        radial-gradient(circle at 88% 0%, rgba(84, 209, 138, 0.12), transparent 28rem),
        linear-gradient(145deg, #070b10 0%, #0c1720 46%, #08131a 100%);
      color: var(--text);
      min-height: 100vh;
      letter-spacing: 0;
    }
    button, input, select { font: inherit; }
    .shell { width: min(1560px, calc(100vw - 32px)); margin: 0 auto 48px; }
    .hero {
      min-height: 280px;
      display: grid;
      align-items: end;
      padding: 36px 0 22px;
    }
    .hero h1 {
      margin: 0;
      max-width: 1050px;
      font-size: clamp(42px, 6vw, 78px);
      line-height: 0.96;
      font-weight: 800;
      letter-spacing: 0;
    }
    .hero p {
      max-width: 880px;
      margin: 18px 0 0;
      color: var(--muted);
      font-size: 17px;
      line-height: 1.6;
    }
    .status-strip {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 22px;
    }
    .pill {
      border: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.06);
      border-radius: 999px;
      padding: 8px 12px;
      color: #dfeaf5;
      font-size: 13px;
      white-space: nowrap;
    }
    .tabs {
      position: sticky;
      top: 0;
      z-index: 20;
      display: flex;
      gap: 8px;
      overflow-x: auto;
      padding: 10px 0;
      background: rgba(7, 11, 16, 0.78);
      backdrop-filter: blur(18px);
      border-bottom: 1px solid rgba(149, 164, 182, 0.12);
    }
    .tab-button {
      border: 1px solid transparent;
      border-radius: 8px;
      color: var(--muted);
      background: transparent;
      padding: 10px 12px;
      cursor: pointer;
      white-space: nowrap;
      transition: background 160ms ease, color 160ms ease, border 160ms ease;
    }
    .tab-button:hover, .tab-button.active {
      color: var(--text);
      border-color: var(--line);
      background: rgba(255, 255, 255, 0.08);
    }
    main { padding-top: 18px; }
    .tab-panel { display: none; animation: rise 220ms ease both; }
    .tab-panel.active { display: block; }
    @keyframes rise {
      from { opacity: 0; transform: translateY(8px); }
      to { opacity: 1; transform: translateY(0); }
    }
    .grid { display: grid; gap: 16px; }
    .metrics-grid { grid-template-columns: repeat(4, minmax(180px, 1fr)); }
    .two-col { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); }
    .three-col { grid-template-columns: repeat(3, minmax(0, 1fr)); }
    .card {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(18px);
    }
    .card-pad { padding: 18px; }
    .metric-card { min-height: 132px; padding: 18px; display: grid; align-content: space-between; }
    .metric-label { color: var(--muted); font-size: 13px; text-transform: uppercase; letter-spacing: 0; }
    .metric-value { margin-top: 14px; font-weight: 800; font-size: clamp(26px, 3vw, 40px); }
    .metric-note { margin-top: 8px; color: var(--muted); font-size: 13px; line-height: 1.4; }
    .positive { color: var(--green); }
    .negative { color: var(--red); }
    .neutral { color: var(--amber); }
    .section-title {
      display: flex;
      justify-content: space-between;
      gap: 14px;
      align-items: end;
      margin: 24px 0 12px;
    }
    .section-title h2 { margin: 0; font-size: 25px; letter-spacing: 0; }
    .section-title p { margin: 6px 0 0; color: var(--muted); line-height: 1.5; max-width: 760px; }
    .chart { width: 100%; height: 470px; }
    .chart.tall { height: 620px; }
    .chart.short { height: 340px; }
    .table-wrap { width: 100%; overflow-x: auto; }
    table { width: 100%; border-collapse: collapse; min-width: 760px; }
    th, td { padding: 11px 12px; border-bottom: 1px solid rgba(149, 164, 182, 0.16); text-align: left; vertical-align: top; }
    th { color: #d4e2ef; font-size: 12px; text-transform: uppercase; letter-spacing: 0; background: rgba(255, 255, 255, 0.04); }
    td { color: #e8f0f7; font-size: 13px; }
    tr.clickable { cursor: pointer; }
    tr.clickable:hover { background: rgba(95, 179, 255, 0.08); }
    .toolbar { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin-bottom: 12px; }
    .control {
      border: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.07);
      color: var(--text);
      border-radius: 8px;
      padding: 9px 11px;
      min-height: 40px;
    }
    .icon-button, .text-button {
      border: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.08);
      color: var(--text);
      border-radius: 8px;
      min-height: 40px;
      padding: 9px 12px;
      cursor: pointer;
    }
    .icon-button:disabled, .text-button:disabled { opacity: 0.45; cursor: not-allowed; }
    .info-grid { grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); }
    .info-card h3 { margin: 0 0 8px; font-size: 17px; }
    .info-card p { margin: 0 0 8px; color: var(--muted); line-height: 1.5; }
    details {
      border: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.05);
      border-radius: 8px;
      padding: 13px 15px;
    }
    details + details { margin-top: 10px; }
    summary { cursor: pointer; font-weight: 700; }
    .small { color: var(--muted); font-size: 12px; line-height: 1.45; }
    .modal {
      position: fixed;
      inset: 0;
      background: rgba(0, 0, 0, 0.68);
      display: none;
      align-items: center;
      justify-content: center;
      padding: 28px;
      z-index: 80;
    }
    .modal.open { display: flex; }
    .modal-panel {
      width: min(1260px, 100%);
      max-height: calc(100vh - 48px);
      overflow: auto;
      background: var(--panel-strong);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      padding: 18px;
    }
    .modal-head { display: flex; justify-content: space-between; gap: 12px; align-items: start; }
    .modal-head h2 { margin: 0; }
    .empty {
      padding: 36px;
      color: var(--muted);
      text-align: center;
      border: 1px dashed var(--line);
      border-radius: 8px;
      background: rgba(255, 255, 255, 0.04);
    }
    @media (max-width: 960px) {
      .metrics-grid, .two-col, .three-col { grid-template-columns: 1fr; }
      .chart { height: 380px; }
      .hero { min-height: 230px; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <section class="hero">
      <div>
        <h1>Polymarket Event Trading Dashboard</h1>
        <p>Backtest performance, strategy behavior, market-to-asset reasoning, risk, model diagnostics, and every trade in one regenerated report.</p>
        <div class="status-strip" id="status-strip"></div>
      </div>
    </section>
    <nav class="tabs" id="tabs"></nav>
    <main id="panels"></main>
  </div>
  <div class="modal" id="trade-modal" role="dialog" aria-modal="true">
    <div class="modal-panel">
      <div class="modal-head">
        <div>
          <h2 id="modal-title">Trade Detail</h2>
          <p class="small" id="modal-subtitle"></p>
        </div>
        <button class="icon-button" type="button" onclick="closeTradeModal()" title="Close">X</button>
      </div>
      <div id="trade-detail-chart" class="chart tall"></div>
      <div id="trade-detail-meta" class="grid three-col"></div>
    </div>
  </div>
  <script>
    const DATA = __DASHBOARD_DATA__;
    const tabs = [
      ["overview", "Overview"],
      ["strategies", "Strategies"],
      ["earnings", "Earnings Split"],
      ["portfolio", "Portfolio $100K"],
      ["risk", "Risk Metrics"],
      ["rf", "RF Model"],
      ["explorer", "Trade Explorer"],
      ["experiments", "Experiment History"]
    ];
    const colors = {
      blue: "#5fb3ff",
      green: "#54d18a",
      red: "#ff6b6b",
      amber: "#ffd166",
      violet: "#b794ff",
      cyan: "#46d9d0",
      muted: "#a9b8c7"
    };
    const plotLayout = {
      paper_bgcolor: "rgba(0,0,0,0)",
      plot_bgcolor: "rgba(0,0,0,0)",
      font: {family: "Inter, sans-serif", color: "#dfeaf5"},
      margin: {l: 72, r: 32, t: 46, b: 72},
      hovermode: "x unified",
      xaxis: {gridcolor: "rgba(149,164,182,0.16)", zerolinecolor: "rgba(149,164,182,0.2)"},
      yaxis: {gridcolor: "rgba(149,164,182,0.16)", zerolinecolor: "rgba(149,164,182,0.2)"},
      legend: {orientation: "h", y: 1.08, x: 0}
    };
    const plotConfig = {responsive: true, displaylogo: false, modeBarButtonsToRemove: ["lasso2d", "select2d"]};
    let explorerState = {page: 1, pageSize: 50, strategy: "all", query: "", sort: "entry_at"};
    let riskStrategy = "all";

    function money(value) {
      const n = Number(value || 0);
      return n.toLocaleString(undefined, {style: "currency", currency: "USD", maximumFractionDigits: 0});
    }
    function pct(value, digits = 1) {
      if (value === null || value === undefined || Number.isNaN(Number(value))) return "n/a";
      return `${(Number(value) * 100).toFixed(digits)}%`;
    }
    function num(value, digits = 2) {
      if (value === null || value === undefined || Number.isNaN(Number(value))) return "n/a";
      return Number(value).toFixed(digits);
    }
    function cls(value, goodPositive = true) {
      const n = Number(value || 0);
      if (Math.abs(n) < 1e-9) return "neutral";
      return (goodPositive ? n > 0 : n < 0) ? "positive" : "negative";
    }
    function el(html) {
      const template = document.createElement("template");
      template.innerHTML = html.trim();
      return template.content.firstElementChild;
    }
    function setHtml(id, html) {
      document.getElementById(id).innerHTML = html;
    }
    function strategyOptions() {
      return [...new Set((DATA.trades || []).map(t => t.strategy_branch || "unknown"))].sort();
    }
    function strategyName(key) {
      const raw = String(key || "default").toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
      const desc = DATA.strategy_descriptions[raw] || DATA.strategy_descriptions[
        raw.includes("sharpe") && raw.includes("rf") ? "cem_sharpe_rf" :
        raw.includes("rf") || raw.includes("machine_learning") ? "rf" :
        raw.includes("sharpe") ? "cem_sharpe" :
        raw.includes("cem") ? "cem" :
        raw.includes("momentum") || raw.includes("poly") ? "momentum" : "default"
      ];
      return desc ? desc.name : key;
    }
    function tradeReturn(trade) {
      const entry = Number(trade.entry_price || 0);
      const exit = Number(trade.exit_price || trade.final_mark_price || entry);
      const direction = trade.direction === "short" ? -1 : 1;
      return entry ? ((exit - entry) / entry) * direction : 0;
    }

    function init() {
      document.getElementById("tabs").innerHTML = tabs.map(([id, label], index) =>
        `<button class="tab-button ${index === 0 ? "active" : ""}" type="button" data-tab="${id}">${label}</button>`
      ).join("");
      document.getElementById("panels").innerHTML = tabs.map(([id], index) =>
        `<section class="tab-panel ${index === 0 ? "active" : ""}" id="panel-${id}"></section>`
      ).join("");
      document.querySelectorAll(".tab-button").forEach(button => {
        button.addEventListener("click", () => showTab(button.dataset.tab));
      });
      renderStatus();
      renderOverview();
      renderStrategies();
      renderEarnings();
      renderPortfolio();
      renderRisk();
      renderRF();
      renderExplorer();
      renderExperiments();
    }
    function showTab(id) {
      document.querySelectorAll(".tab-button").forEach(button => button.classList.toggle("active", button.dataset.tab === id));
      document.querySelectorAll(".tab-panel").forEach(panel => panel.classList.toggle("active", panel.id === `panel-${id}`));
      setTimeout(() => window.dispatchEvent(new Event("resize")), 30);
    }
    function renderStatus() {
      const summary = DATA.summary || {};
      const perf = summary.overall_performance || {};
      const strip = document.getElementById("status-strip");
      strip.innerHTML = [
        `Generated ${DATA.generated_at}`,
        `${DATA.trades.length} trades`,
        `${money(perf.total_net_profit || 0)} net P&L`,
        `${pct(perf.win_rate || 0)} win rate`,
        DATA.experiments.source ? `Experiments: ${DATA.experiments.source}` : "Experiment log missing"
      ].map(text => `<span class="pill">${text}</span>`).join("");
    }
    function metric(label, value, note, className = "") {
      return `<div class="card metric-card"><div><div class="metric-label">${label}</div><div class="metric-value ${className}">${value}</div></div><div class="metric-note">${note || ""}</div></div>`;
    }

    function renderOverview() {
      const perf = (DATA.summary || {}).overall_performance || {};
      const panel = document.getElementById("panel-overview");
      panel.innerHTML = `
        <div class="grid metrics-grid">
          ${metric("Total Net Profit", money(perf.total_net_profit || 0), "Across all executable trades", cls(perf.total_net_profit))}
          ${metric("Trade Count", DATA.trades.length.toLocaleString(), "Every trade is available in the explorer")}
          ${metric("Win Rate", pct(perf.win_rate || 0), "Share of profitable exits", cls((perf.win_rate || 0) - 0.5))}
          ${metric("Profit Factor", num(perf.profit_factor), "Gross profit / gross loss", cls((perf.profit_factor || 0) - 1))}
        </div>
        <div class="section-title"><div><h2>Strategy Comparison</h2><p>Net profit, win rate, and trade count by policy branch.</p></div></div>
        <div class="card card-pad"><div id="overview-strategy-chart" class="chart"></div></div>
        <div class="section-title"><div><h2>Processing Funnel</h2><p>The pipeline path from market filtering through executable trades.</p></div></div>
        <div class="card card-pad"><div id="funnel-chart" class="chart short"></div></div>
      `;
      const rows = DATA.strategy_summaries || [];
      Plotly.newPlot("overview-strategy-chart", [
        {type: "bar", name: "Net P&L", x: rows.map(r => r.strategy), y: rows.map(r => r.total_net_profit), marker: {color: colors.blue}, yaxis: "y"},
        {type: "scatter", mode: "lines+markers", name: "Win rate", x: rows.map(r => r.strategy), y: rows.map(r => (r.win_rate || 0) * 100), marker: {color: colors.green}, yaxis: "y2"}
      ], {...plotLayout, yaxis: {title: "Net P&L ($)", gridcolor: "rgba(149,164,182,0.16)"}, yaxis2: {title: "Win rate (%)", overlaying: "y", side: "right"}, xaxis: {tickangle: -25, gridcolor: "rgba(149,164,182,0.08)"}, title: "Performance by strategy"}, plotConfig);
      const funnel = DATA.funnel || {};
      const labels = Object.keys(funnel);
      Plotly.newPlot("funnel-chart", [{type: "bar", orientation: "h", x: labels.map(k => funnel[k]), y: labels, marker: {color: colors.cyan}}], {...plotLayout, margin: {l: 210, r: 24, t: 34, b: 46}, title: "Event Processing Funnel"}, plotConfig);
    }

    function renderStrategies() {
      const rows = DATA.strategy_summaries || [];
      const cards = rows.map(row => {
        const descKey = Object.keys(DATA.strategy_descriptions).find(k => strategyName(row.strategy) === DATA.strategy_descriptions[k].name);
        const desc = DATA.strategy_descriptions[descKey] || DATA.strategy_descriptions.default;
        return `<details open><summary>${row.strategy} - ${desc.name}</summary>
          <div class="grid two-col" style="margin-top:14px">
            <div><p>${desc.description}</p><p class="small"><b>Optimizes:</b> ${desc.optimizes}</p><p class="small"><b>Parameters:</b> ${desc.parameters}</p><p class="small"><b>When to use:</b> ${desc.use}</p></div>
            <div class="grid metrics-grid" style="grid-template-columns: repeat(2, minmax(120px, 1fr)); box-shadow:none">
              ${metric("Net P&L", money(row.total_net_profit), "", cls(row.total_net_profit))}
              ${metric("Win Rate", pct(row.win_rate || 0), "")}
            </div>
          </div>
        </details>`;
      }).join("");
      document.getElementById("panel-strategies").innerHTML = `
        <div class="section-title"><div><h2>Strategy Playbook</h2><p>Each policy is shown with what it is trying to optimize, how it behaves, and when it should earn its place.</p></div></div>
        <div class="grid">${cards || `<div class="empty">No strategy rows were found.</div>`}</div>
        <div class="section-title"><div><h2>Distribution by Strategy</h2><p>Trade-level returns make the difference between a smooth strategy and a lucky total.</p></div></div>
        <div class="card card-pad"><div id="strategy-return-box" class="chart"></div></div>
      `;
      const grouped = strategyOptions();
      Plotly.newPlot("strategy-return-box", grouped.map(name => ({
        type: "box",
        name,
        y: DATA.trades.filter(t => (t.strategy_branch || "unknown") === name).map(tradeReturn).map(v => v * 100),
        boxpoints: "outliers"
      })), {...plotLayout, title: "Trade return distribution", yaxis: {title: "Return (%)", gridcolor: "rgba(149,164,182,0.16)"}}, plotConfig);
    }

    function renderEarnings() {
      const rows = DATA.archetype_rows || [];
      const body = rows.map(row => `
        <tr class="clickable" onclick="showArchetype('${String(row.event_archetype).replace(/'/g, "\\'")}')">
          <td>${row.event_archetype}</td>
          <td>${row.trade_count}</td>
          <td class="${cls(row.total_net_profit)}">${money(row.total_net_profit)}</td>
          <td>${pct(row.win_rate || 0)}</td>
          <td>${row.sample_symbol || ""}</td>
          <td>${row.sample_question || ""}</td>
        </tr>
      `).join("");
      document.getElementById("panel-earnings").innerHTML = `
        <div class="section-title"><div><h2>Archetype Deep Dive</h2><p>From Polymarket question to LLM world mapping to traded asset. Click an archetype row to inspect the journey examples in the Trade Explorer.</p></div></div>
        <div class="card table-wrap"><table><thead><tr><th>Archetype</th><th>Trades</th><th>Net P&L</th><th>Win Rate</th><th>Sample Asset</th><th>Sample Question</th></tr></thead><tbody>${body}</tbody></table></div>
        <div class="section-title"><div><h2>Archetype P&L</h2><p>Which event categories actually paid for their complexity?</p></div></div>
        <div class="card card-pad"><div id="archetype-chart" class="chart"></div></div>
      `;
      Plotly.newPlot("archetype-chart", [{type: "bar", x: rows.map(r => r.event_archetype), y: rows.map(r => r.total_net_profit), marker: {color: rows.map(r => Number(r.total_net_profit) >= 0 ? colors.green : colors.red)}}], {...plotLayout, title: "Net P&L by archetype", xaxis: {tickangle: -25, gridcolor: "rgba(149,164,182,0.08)"}, yaxis: {title: "Net P&L ($)", gridcolor: "rgba(149,164,182,0.16)"}}, plotConfig);
    }
    function showArchetype(name) {
      explorerState.query = name;
      showTab("explorer");
      renderExplorer();
    }

    function normalizedBenchmark(symbol) {
      const rows = (DATA.benchmark_daily || []).filter(r => r.symbol === symbol || r.path_symbol === symbol);
      if (!rows.length) return {x: [], y: []};
      const first = Number(rows[0].close || 0);
      return {x: rows.map(r => r.date || String(r.timestamp).slice(0, 10)), y: rows.map(r => first ? Number(r.close) / first * 100000 : null)};
    }
    function renderPortfolio() {
      const equity = DATA.equity || [];
      document.getElementById("panel-portfolio").innerHTML = `
        <div class="section-title"><div><h2>$100K Portfolio</h2><p>Realized equity, drawdown, SPY/QQQ comparison, monthly returns, and trade P&L shape.</p></div></div>
        <div class="card card-pad"><div id="equity-chart" class="chart"></div></div>
        <div class="card card-pad" style="margin-top:16px"><div id="drawdown-chart" class="chart short"></div></div>
        <div class="grid two-col" style="margin-top:16px">
          <div class="card card-pad"><div id="monthly-chart" class="chart"></div></div>
          <div class="card card-pad"><div id="pnl-histogram" class="chart"></div></div>
        </div>
        <div class="card card-pad" style="margin-top:16px"><div id="positions-chart" class="chart short"></div></div>
      `;
      const spy = normalizedBenchmark("SPY");
      const qqq = normalizedBenchmark("QQQ");
      Plotly.newPlot("equity-chart", [
        {type: "scatter", mode: "lines", name: "Strategy equity", x: equity.map(r => r.date), y: equity.map(r => r.equity), line: {color: colors.blue, width: 3}},
        {type: "scatter", mode: "lines", name: "SPY buy & hold", x: spy.x, y: spy.y, line: {color: colors.amber, width: 2}},
        {type: "scatter", mode: "lines", name: "QQQ buy & hold", x: qqq.x, y: qqq.y, line: {color: colors.violet, width: 2}}
      ], {...plotLayout, title: "Equity curve with benchmarks", yaxis: {title: "Portfolio value ($)", gridcolor: "rgba(149,164,182,0.16)"}}, plotConfig);
      Plotly.newPlot("drawdown-chart", [{type: "scatter", mode: "lines", fill: "tozeroy", name: "Drawdown", x: equity.map(r => r.date), y: equity.map(r => r.drawdown), line: {color: colors.red}}], {...plotLayout, title: "Drawdown", yaxis: {title: "Drawdown ($)", gridcolor: "rgba(149,164,182,0.16)"}}, plotConfig);
      const monthMap = {};
      equity.forEach(r => { const m = String(r.date).slice(0, 7); monthMap[m] = (monthMap[m] || 0) + Number(r.pnl || 0); });
      const months = Object.keys(monthMap).sort();
      Plotly.newPlot("monthly-chart", [{type: "bar", x: months, y: months.map(m => monthMap[m]), marker: {color: months.map(m => monthMap[m] >= 0 ? colors.green : colors.red)}}], {...plotLayout, title: "Monthly realized P&L", yaxis: {title: "P&L ($)", gridcolor: "rgba(149,164,182,0.16)"}}, plotConfig);
      Plotly.newPlot("pnl-histogram", [{type: "histogram", x: DATA.trades.map(t => Number(t.net_profit || 0)), marker: {color: colors.cyan}}], {...plotLayout, title: "P&L distribution", xaxis: {title: "Trade P&L ($)", gridcolor: "rgba(149,164,182,0.16)"}, yaxis: {title: "Count", gridcolor: "rgba(149,164,182,0.16)"}}, plotConfig);
      const byDate = {};
      DATA.trades.forEach(t => {
        const start = new Date(t.entry_at);
        const end = new Date(t.exit_at || t.entry_at);
        for (let d = new Date(start); d <= end; d.setDate(d.getDate() + 1)) {
          const key = d.toISOString().slice(0, 10);
          byDate[key] = (byDate[key] || 0) + 1;
        }
      });
      const posDates = Object.keys(byDate).sort();
      Plotly.newPlot("positions-chart", [{type: "scatter", mode: "lines", fill: "tozeroy", name: "Open trades", x: posDates, y: posDates.map(d => byDate[d]), line: {color: colors.green}}], {...plotLayout, title: "Open positions over time", yaxis: {title: "Open trade count", gridcolor: "rgba(149,164,182,0.16)"}}, plotConfig);
    }

    function renderRisk() {
      const options = ["all", ...strategyOptions()].map(s => `<option value="${s}">${s === "all" ? "All strategies" : s}</option>`).join("");
      document.getElementById("panel-risk").innerHTML = `
        <div class="section-title"><div><h2>Risk Metrics</h2><p>Traffic-light risk context plus clickable winners and losers.</p></div></div>
        <div class="grid info-grid">${DATA.risk_explanations.map(item => `<div class="card card-pad info-card"><h3>${item.metric}</h3><p>${item.meaning}</p><p class="small">${item.good}</p></div>`).join("")}</div>
        <div class="section-title"><div><h2>Risk Table</h2></div></div>
        <div class="card table-wrap" id="risk-table"></div>
        <div class="section-title"><div><h2>Winners and Losers</h2><p>Switch strategy, then click any trade for the normalized asset, SPY, QQQ, and Polymarket probability path.</p></div><select class="control" id="risk-strategy">${options}</select></div>
        <div class="grid two-col"><div class="card table-wrap" id="best-table"></div><div class="card table-wrap" id="worst-table"></div></div>
      `;
      document.getElementById("risk-strategy").value = riskStrategy;
      document.getElementById("risk-strategy").addEventListener("change", event => {
        riskStrategy = event.target.value;
        renderWinnerLoserTables();
      });
      renderRiskTable();
      renderWinnerLoserTables();
    }
    function riskClass(metric, value) {
      const n = Number(value);
      if (value === null || Number.isNaN(n)) return "neutral";
      if (["max_drawdown", "var_95", "cvar_95"].includes(metric)) return n > -0.1 ? "positive" : n > -0.3 ? "neutral" : "negative";
      if (metric === "profit_factor") return n > 1.5 ? "positive" : n > 1 ? "neutral" : "negative";
      return n > 2 ? "positive" : n > 1 ? "neutral" : "negative";
    }
    function renderRiskTable() {
      const rows = DATA.risk_rows || [];
      document.getElementById("risk-table").innerHTML = `<table><thead><tr><th>Strategy</th><th>Trades</th><th>Sharpe</th><th>Sortino</th><th>Max DD</th><th>Calmar</th><th>VaR 95</th><th>CVaR 95</th><th>Profit Factor</th></tr></thead><tbody>${rows.map(r => `
        <tr><td>${r.strategy}</td><td>${r.trade_count}</td><td class="${riskClass("sharpe", r.sharpe)}">${num(r.sharpe)}</td><td class="${riskClass("sortino", r.sortino)}">${num(r.sortino)}</td><td class="${riskClass("max_drawdown", r.max_drawdown)}">${pct(r.max_drawdown)}</td><td class="${riskClass("calmar", r.calmar)}">${num(r.calmar)}</td><td class="${riskClass("var_95", r.var_95)}">${pct(r.var_95)}</td><td class="${riskClass("cvar_95", r.cvar_95)}">${pct(r.cvar_95)}</td><td class="${riskClass("profit_factor", r.profit_factor)}">${num(r.profit_factor)}</td></tr>`).join("")}</tbody></table>`;
    }
    function renderWinnerLoserTables() {
      const pool = DATA.trades.filter(t => riskStrategy === "all" || (t.strategy_branch || "unknown") === riskStrategy);
      const sorted = [...pool].sort((a, b) => Number(b.net_profit || 0) - Number(a.net_profit || 0));
      document.getElementById("best-table").innerHTML = tradeMiniTable("Best 15", sorted.slice(0, 15));
      document.getElementById("worst-table").innerHTML = tradeMiniTable("Worst 15", sorted.slice(-15).reverse());
    }
    function tradeMiniTable(title, rows) {
      return `<table><thead><tr><th colspan="5">${title}</th></tr><tr><th>Date</th><th>Symbol</th><th>Strategy</th><th>P&L</th><th>Exit</th></tr></thead><tbody>${rows.map(t => `<tr class="clickable" onclick="openTradeModal('${t.trade_id}')"><td>${String(t.entry_at || "").slice(0, 10)}</td><td>${t.symbol}</td><td>${t.strategy_branch}</td><td class="${cls(t.net_profit)}">${money(t.net_profit)}</td><td>${t.exit_reason || ""}</td></tr>`).join("")}</tbody></table>`;
    }

    function renderRF() {
      const metrics = DATA.ml_metrics || {};
      document.getElementById("panel-rf").innerHTML = `
        <div class="section-title"><div><h2>Random Forest Model</h2><p>The RF is a trade-quality filter. It predicts whether a candidate should be profitable and lets through only trades with a positive expected outcome.</p></div></div>
        <div class="grid metrics-grid">
          ${metric("Direction Accuracy", pct(metrics.classification_accuracy || 0), "50% is random direction guessing", cls((metrics.classification_accuracy || 0) - 0.5))}
          ${metric("Long Precision", pct(metrics.long_precision || 0), "How often predicted long trades were actually long winners")}
          ${metric("Short Precision", pct(metrics.short_precision || 0), "How often predicted short trades were actually short winners")}
          ${metric("Goal Hit Rate", pct(metrics.predicted_goal_hit_rate || 0), "Predicted target reached")}
        </div>
        <div class="grid two-col" style="margin-top:16px">
          <div class="card card-pad"><div id="feature-chart" class="chart"></div></div>
          <div class="card card-pad"><div id="rf-split-chart" class="chart"></div></div>
        </div>
      `;
      const featureTotals = {};
      (DATA.snapshots || []).forEach(s => {
        const coeffs = s.ridge_coefficients || s.classifier_coefficients || {};
        Object.entries(coeffs).forEach(([key, value]) => { featureTotals[key] = (featureTotals[key] || 0) + Math.abs(Number(value || 0)); });
      });
      const featureRows = Object.entries(featureTotals).sort((a, b) => b[1] - a[1]).slice(0, 12);
      Plotly.newPlot("feature-chart", [{type: "bar", orientation: "h", y: featureRows.map(r => r[0]).reverse(), x: featureRows.map(r => r[1]).reverse(), marker: {color: colors.blue}}], {...plotLayout, title: "Feature importance proxy", margin: {l: 220, r: 24, t: 42, b: 54}}, plotConfig);
      const splitRows = (DATA.snapshots || []).slice(-20).map((s, i) => {
        const vm = s.validation_metrics || {};
        return {name: `${s.symbol || "split"} ${i + 1}`, accuracy: Number(vm.accuracy || vm.classification_accuracy || 0), samples: Number(s.training_sample_count || 0)};
      });
      Plotly.newPlot("rf-split-chart", [
        {type: "bar", name: "Accuracy", x: splitRows.map(r => r.name), y: splitRows.map(r => r.accuracy * 100), marker: {color: colors.green}},
        {type: "scatter", mode: "lines+markers", name: "Training samples", x: splitRows.map(r => r.name), y: splitRows.map(r => r.samples), yaxis: "y2", marker: {color: colors.amber}}
      ], {...plotLayout, title: "Split-by-split model context", xaxis: {tickangle: -35, gridcolor: "rgba(149,164,182,0.08)"}, yaxis: {title: "Accuracy (%)", gridcolor: "rgba(149,164,182,0.16)"}, yaxis2: {title: "Samples", overlaying: "y", side: "right"}}, plotConfig);
    }

    function renderExplorer() {
      const options = ["all", ...strategyOptions()].map(s => `<option value="${s}">${s === "all" ? "All strategies" : s}</option>`).join("");
      document.getElementById("panel-explorer").innerHTML = `
        <div class="section-title"><div><h2>Trade Explorer</h2><p>All trades are paginated, filterable, sortable, and clickable.</p></div></div>
        <div class="toolbar">
          <select class="control" id="explorer-strategy">${options}</select>
          <input class="control" id="explorer-query" placeholder="Search symbol, archetype, question" value="${explorerState.query || ""}">
          <select class="control" id="explorer-sort">
            <option value="entry_at">Entry date</option>
            <option value="net_profit">P&L</option>
            <option value="symbol">Symbol</option>
          </select>
        </div>
        <div class="card table-wrap" id="explorer-table"></div>
        <div class="toolbar" style="justify-content:center; margin-top:12px">
          <button class="text-button" id="prev-page" type="button">Previous</button>
          <span class="pill" id="page-label"></span>
          <button class="text-button" id="next-page" type="button">Next</button>
        </div>
      `;
      document.getElementById("explorer-strategy").value = explorerState.strategy;
      document.getElementById("explorer-sort").value = explorerState.sort;
      document.getElementById("explorer-strategy").addEventListener("change", e => { explorerState.strategy = e.target.value; explorerState.page = 1; renderExplorerTable(); });
      document.getElementById("explorer-query").addEventListener("input", e => { explorerState.query = e.target.value; explorerState.page = 1; renderExplorerTable(); });
      document.getElementById("explorer-sort").addEventListener("change", e => { explorerState.sort = e.target.value; renderExplorerTable(); });
      document.getElementById("prev-page").addEventListener("click", () => { explorerState.page = Math.max(1, explorerState.page - 1); renderExplorerTable(); });
      document.getElementById("next-page").addEventListener("click", () => { explorerState.page += 1; renderExplorerTable(); });
      renderExplorerTable();
    }
    function filteredTrades() {
      const q = String(explorerState.query || "").toLowerCase();
      return DATA.trades.filter(t => {
        if (explorerState.strategy !== "all" && (t.strategy_branch || "unknown") !== explorerState.strategy) return false;
        if (!q) return true;
        return [t.symbol, t.event_archetype, t.question, t.market_id, t.exit_reason].join(" ").toLowerCase().includes(q);
      }).sort((a, b) => {
        if (explorerState.sort === "net_profit") return Number(b.net_profit || 0) - Number(a.net_profit || 0);
        return String(a[explorerState.sort] || "").localeCompare(String(b[explorerState.sort] || ""));
      });
    }
    function renderExplorerTable() {
      const rows = filteredTrades();
      const pages = Math.max(1, Math.ceil(rows.length / explorerState.pageSize));
      explorerState.page = Math.min(explorerState.page, pages);
      const start = (explorerState.page - 1) * explorerState.pageSize;
      const pageRows = rows.slice(start, start + explorerState.pageSize);
      document.getElementById("page-label").textContent = `Page ${explorerState.page} of ${pages} - ${rows.length} trades`;
      document.getElementById("prev-page").disabled = explorerState.page <= 1;
      document.getElementById("next-page").disabled = explorerState.page >= pages;
      document.getElementById("explorer-table").innerHTML = `<table><thead><tr><th>Entry</th><th>Symbol</th><th>Strategy</th><th>Archetype</th><th>Return</th><th>Net P&L</th><th>vs SPY</th><th>vs QQQ</th><th>Question</th></tr></thead><tbody>${pageRows.map(t => `<tr class="clickable" onclick="openTradeModal('${t.trade_id}')"><td>${String(t.entry_at || "").slice(0, 10)}</td><td>${t.symbol}</td><td>${t.strategy_branch}</td><td>${t.event_archetype || ""}</td><td class="${cls(tradeReturn(t))}">${pct(tradeReturn(t))}</td><td class="${cls(t.net_profit)}">${money(t.net_profit)}</td><td>${pct(t.excess_spy || t.spy_return || null)}</td><td>${pct(t.excess_qqq || t.qqq_return || null)}</td><td>${t.question || ""}</td></tr>`).join("")}</tbody></table>`;
    }

    function renderExperiments() {
      const exp = DATA.experiments || {};
      if (exp.missing) {
        document.getElementById("panel-experiments").innerHTML = `<div class="section-title"><div><h2>Experiment History</h2></div></div><div class="empty">${exp.missing}</div>`;
        return;
      }
      document.getElementById("panel-experiments").innerHTML = `
        <div class="section-title"><div><h2>Experiment History</h2><p>${exp.source || ""}</p></div></div>
        <div class="grid">${(exp.phases || []).map(phase => `<details open><summary>${phase.title}</summary><div style="margin-top:12px">${(phase.body || []).map(p => `<p>${p}</p>`).join("")}${(phase.items || []).map(item => `<details><summary>${item.title}</summary>${(item.details || []).map(d => `<p class="small">${d}</p>`).join("")}</details>`).join("")}</div></details>`).join("")}</div>
      `;
    }

    function normalizedPath(trade, symbol) {
      const rows = (DATA.trade_paths[String(trade.trade_id)] || []).filter(r => r.symbol === symbol);
      if (!rows.length) return null;
      const first = Number(rows[0].close || 0);
      return {x: rows.map(r => r.timestamp), y: rows.map(r => first ? Number(r.close) / first * 100 : null)};
    }
    function fallbackTradePath(trade) {
      const entry = Number(trade.entry_price || 0);
      const exit = Number(trade.exit_price || trade.final_mark_price || entry);
      return {
        x: [trade.entry_at, trade.exit_at || trade.entry_at],
        y: [100, entry ? exit / entry * 100 : 100]
      };
    }
    function openTradeModal(tradeId) {
      const trade = DATA.trades.find(t => String(t.trade_id) === String(tradeId));
      if (!trade) return;
      document.getElementById("trade-modal").classList.add("open");
      document.getElementById("modal-title").textContent = `${trade.symbol} - ${money(trade.net_profit)} - ${trade.strategy_branch}`;
      document.getElementById("modal-subtitle").textContent = trade.question || "";
      const asset = normalizedPath(trade, trade.symbol) || fallbackTradePath(trade);
      const spy = normalizedPath(trade, "SPY");
      const qqq = normalizedPath(trade, "QQQ");
      const prob = DATA.probability_paths[String(trade.trade_id)] || [];
      const traces = [
        {type: "scatter", mode: "lines", name: `${trade.symbol} normalized`, x: asset.x, y: asset.y, line: {color: colors.blue, width: 3}},
        {type: "scatter", mode: "markers", name: "Entry", x: [trade.entry_at], y: [100], marker: {color: colors.green, size: 11, symbol: trade.direction === "short" ? "triangle-down" : "triangle-up"}},
        {type: "scatter", mode: "markers", name: "Exit", x: [trade.exit_at || trade.entry_at], y: [asset.y[asset.y.length - 1]], marker: {color: colors.red, size: 11, symbol: "x"}}
      ];
      if (spy) traces.push({type: "scatter", mode: "lines", name: "SPY normalized", x: spy.x, y: spy.y, line: {color: colors.amber, width: 2}});
      if (qqq) traces.push({type: "scatter", mode: "lines", name: "QQQ normalized", x: qqq.x, y: qqq.y, line: {color: colors.violet, width: 2}});
      if (prob.length) traces.push({type: "scatter", mode: "lines", name: "Polymarket probability", x: prob.map(p => p.timestamp), y: prob.map(p => Number(p.probability || 0) * 100), yaxis: "y2", line: {color: colors.cyan, dash: "dot"}});
      Plotly.newPlot("trade-detail-chart", traces, {...plotLayout, title: "Trade path vs SPY/QQQ", yaxis: {title: "Normalized to 100 at entry", gridcolor: "rgba(149,164,182,0.16)"}, yaxis2: {title: "Probability (%)", overlaying: "y", side: "right", range: [0, 100]}}, plotConfig);
      document.getElementById("trade-detail-meta").innerHTML = [
        ["Entry", `${String(trade.entry_at || "").slice(0, 19)} at ${num(trade.entry_price)}`],
        ["Exit", `${String(trade.exit_at || "").slice(0, 19)} at ${num(trade.exit_price || trade.final_mark_price)}`],
        ["Archetype", trade.event_archetype || "unknown"],
        ["Relevance", num(trade.relevance_score)],
        ["Exit Reason", trade.exit_reason || "open"],
        ["LLM World", trade.world_universe || trade.world_reason || "not available"]
      ].map(([label, value]) => `<div class="card card-pad"><div class="metric-label">${label}</div><div class="metric-note">${value}</div></div>`).join("");
    }
    function closeTradeModal() {
      document.getElementById("trade-modal").classList.remove("open");
    }
    document.getElementById("trade-modal").addEventListener("click", event => {
      if (event.target.id === "trade-modal") closeTradeModal();
    });
    init();
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
