# Alpha Model Results

Generated: 2026-06-25T07:31:25.299675+00:00

## Dataset

- Source rows from `historical_ml_observations`: 936
- Deduped valid candidates expected: 936
- Rows written: 936
- Label drops: 0
- Median `|y_hedged|`: +2.97%
- Mean `y_hedged`: +0.09%
- Split counts: {'embargo': 56, 'test': 209, 'train': 404, 'val': 267}

The split uses the 10-trading-day embargo windows explicitly: `train <= 2025-12-31`, `val = 2026-01-10..2026-03-31`, `test >= 2026-04-10`; rows inside the two gaps are marked `embargo`.

## Fundamentals

Caveat: yfinance `.info` is point-in-time-now, not as-of-event, so these features carry mild historical look-ahead.

- Refresh attempted: 0
- Refresh stored: 0
- Refresh failed: 0
- Already current for final run: 0
- Symbols with any fundamentals row: 397 / 404
- Missing symbols, first 30: ['CRCL', 'DEFT', 'ETOR', 'HNGE', 'NMAX', 'PSKY', 'UHAL-B']

Missing numeric feature values were median-imputed after coverage was measured; `ALPHA_RESULTS.md` reports that imputation instead of hiding it.

## Model

- Selected model: {'model': 'xgboost', 'objective': 'reg:pseudohubererror'}
- Success criterion on test: **FAIL** (rank_corr > 0 and top-quintile mean `y_hedged` > 0).
- Train-minus-test rank-corr gap: 0.9884

### Linear Baseline

| split | n | rank_corr | top_q_mean_y | q_spread | mae | mean_y |
|---|---:|---:|---:|---:|---:|---:|
| train | 404 | 0.3800 | +1.81% | +5.83% | +3.29% | -0.55% |
| val | 267 | -0.1776 | +0.33% | -7.35% | +7.62% | +0.76% |
| test | 209 | -0.0704 | -0.23% | -1.87% | +4.89% | +0.34% |

### Alpha Model

| split | n | rank_corr | top_q_mean_y | q_spread | mae | mean_y |
|---|---:|---:|---:|---:|---:|---:|
| train | 404 | 0.9130 | +5.93% | +13.64% | +1.46% | -0.55% |
| val | 267 | -0.2204 | -0.80% | -8.66% | +7.48% | +0.76% |
| test | 209 | -0.0753 | +0.38% | -1.44% | +5.00% | +0.34% |

### Ablations

| removed_group | test_rank_corr | delta_vs_full | test_top_q_mean_y |
|---|---:|---:|---:|
| oracle | -0.1433 | 0.0679 | +0.11% |
| world | -0.0772 | 0.0018 | -0.22% |
| price | -0.0833 | 0.0079 | -0.87% |
| fundamentals | 0.0020 | -0.0773 | +1.07% |
| cross_sectional | -0.0762 | 0.0008 | +0.53% |
| old_ml_momentum | -0.0681 | -0.0073 | +0.96% |

## Contract Notes

- `feat_connection_strength` reads `historical_asset_world_assets.connection_strength` when present; legacy worlds without that field default to `1.0` and should be treated as unscored.
- `pre_drop_earnings_defense=True` means the candidate survives the old manual pre-drop of earnings and defense archetypes.
- Momentum feature uses `historical_momentum_parameter_results.momentum_value` at `range_period=14`, `range_multiplier=3.0`.

## Imputation Counts

```json
{
  "feat_asset_2w_trend": 0,
  "feat_beta": 134,
  "feat_cash_to_marketcap": 130,
  "feat_connection_strength": 0,
  "feat_crossing_latency_days": 0,
  "feat_debt_to_equity": 241,
  "feat_log_market_cap": 126,
  "feat_ml_class_prob": 0,
  "feat_ml_pred_peak": 0,
  "feat_momentum_roc": 0,
  "feat_pre_entry_volume_log": 0,
  "feat_prob_at_trigger": 0,
  "feat_prob_slope_24h": 0,
  "feat_prob_surge_since_t0": 0,
  "feat_prob_volatility": 0,
  "feat_profit_margin": 126,
  "feat_runup_rank": 0,
  "feat_runup_since_t0": 0,
  "feat_sector_1m_trend": 0,
  "feat_size_rank": 126,
  "feat_spy_2w_trend": 0,
  "feat_time_to_resolution_days": 0,
  "feat_world_size": 0,
  "feat_ytd_change": 0
}
```
