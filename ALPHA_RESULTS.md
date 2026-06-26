# Alpha Model Results

Generated: 2026-06-25T14:53:37.567047+00:00

## Dataset

- Source rows from `historical_ml_observations`: 32441
- Deduped valid candidates expected: 1925
- Rows written: 1925
- Label drops: 0
- Median `|y_hedged|`: +2.90%
- Mean `y_hedged`: +0.10%
- Split counts: {'embargo': 115, 'test': 240, 'train': 902, 'val': 668}

The split uses the 10-trading-day embargo windows explicitly: `train <= 2025-12-31`, `val = 2026-01-10..2026-03-31`, `test >= 2026-04-10`; rows inside the two gaps are marked `embargo`.

## Fundamentals

Caveat: yfinance `.info` is point-in-time-now, not as-of-event, so these features carry mild historical look-ahead.

- Refresh attempted: 9
- Refresh stored: 9
- Refresh failed: 0
- Already current for final run: 438
- Symbols with any fundamentals row: 447 / 447
- Missing symbols, first 30: []

Missing numeric feature values were median-imputed after coverage was measured; `ALPHA_RESULTS.md` reports that imputation instead of hiding it.

## Model

- Selected model: {'model': 'xgboost', 'objective': 'reg:pseudohubererror'}
- Success criterion on test: **FAIL** (rank_corr > 0 and top-quintile mean `y_hedged` > 0).
- Train-minus-test rank-corr gap: 0.9048

### Linear Baseline

| split | n | rank_corr | top_q_mean_y | q_spread | mae | mean_y |
|---|---:|---:|---:|---:|---:|---:|
| train | 902 | 0.3680 | +1.56% | +4.46% | +2.96% | -0.54% |
| val | 668 | -0.2371 | -0.94% | -6.84% | +6.53% | +0.93% |
| test | 240 | 0.0313 | +1.12% | +0.55% | +4.73% | +0.50% |

### Alpha Model

| split | n | rank_corr | top_q_mean_y | q_spread | mae | mean_y |
|---|---:|---:|---:|---:|---:|---:|
| train | 902 | 0.8383 | +4.81% | +11.06% | +1.67% | -0.54% |
| val | 668 | -0.1208 | -0.22% | -4.78% | +6.40% | +0.93% |
| test | 240 | -0.0665 | +0.17% | -1.88% | +5.11% | +0.50% |

### Ablations

| removed_group | test_rank_corr | delta_vs_full | test_top_q_mean_y |
|---|---:|---:|---:|
| oracle | -0.0812 | 0.0147 | +0.07% |
| world | -0.0770 | 0.0104 | +0.07% |
| price | 0.0072 | -0.0737 | +1.80% |
| fundamentals | -0.0312 | -0.0353 | +0.35% |
| cross_sectional | -0.0671 | 0.0005 | +0.21% |
| old_ml_momentum | -0.0262 | -0.0403 | +0.75% |

## Contract Notes

- `feat_connection_strength` reads `historical_asset_world_assets.connection_strength` when present; legacy worlds without that field default to `1.0` and should be treated as unscored.
- `pre_drop_earnings_defense=True` means the candidate survives the old manual pre-drop of earnings and defense archetypes.
- Momentum feature uses `historical_momentum_parameter_results.momentum_value` at `range_period=14`, `range_multiplier=3.0`.

## Imputation Counts

```json
{
  "feat_asset_2w_trend": 0,
  "feat_beta": 451,
  "feat_cash_to_marketcap": 443,
  "feat_connection_strength": 0,
  "feat_crossing_latency_days": 0,
  "feat_debt_to_equity": 577,
  "feat_log_market_cap": 436,
  "feat_ml_class_prob": 0,
  "feat_ml_pred_peak": 0,
  "feat_momentum_roc": 0,
  "feat_pre_entry_volume_log": 0,
  "feat_prob_at_trigger": 0,
  "feat_prob_slope_24h": 0,
  "feat_prob_surge_since_t0": 0,
  "feat_prob_volatility": 0,
  "feat_profit_margin": 439,
  "feat_runup_rank": 0,
  "feat_runup_since_t0": 0,
  "feat_sector_1m_trend": 0,
  "feat_size_rank": 436,
  "feat_spy_2w_trend": 0,
  "feat_time_to_resolution_days": 0,
  "feat_world_size": 0,
  "feat_ytd_change": 0
}
```
