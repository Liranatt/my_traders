# Portfolio Layer Results

Generated: 2026-06-24T14:46:16.040840+00:00

## Dataset

- Candidate rows loaded: 1733
- Split counts: {'embargo': 106, 'test': 218, 'train': 829, 'val': 580}
- Alpha mode: `parquet`
- Constant notional per position: `10,000` on `100,000` book
- Max positions: 10; sector cap: 4

The simulator uses the contract candidate table plus raw daily price and probability series. Positions are sector-hedged and exit dynamically; there is no fixed 10-day portfolio-layer cap.

## Selected V1 Policies

Policy grid: `entry_strong_probability in {0.60,0.65,0.70}` x `confirmation_window=2d` x `theta_out in {0.50,0.55}` x stop type/level. Each objective/universe pair is selected on validation; test is evaluated after selection.

| objective | universe | policy | split | trades | sharpe | maxDD | calmar | net | hit | turnover |
|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| sharpe | all | strong>=0.6/conf2d, close_vol_trailing@3, poly<0.55 | train | 242 | 3.747 | +3.52% | 7.378 | +38.79% | +66.53% | 17.035 |
| sharpe | all | strong>=0.6/conf2d, close_vol_trailing@3, poly<0.55 | val | 107 | 1.828 | +4.25% | 5.888 | +6.87% | +50.47% | 35.952 |
| sharpe | all | strong>=0.6/conf2d, close_vol_trailing@3, poly<0.55 | test | 65 | 0.127 | +4.77% | 0.164 | +0.11% | +43.08% | 46.800 |
| sharpe | pre_drop | strong>=0.7/conf2d, fixed@0.08, poly<0.5 | train | 41 | 1.307 | +5.01% | 1.279 | +8.90% | +80.49% | 2.986 |
| sharpe | pre_drop | strong>=0.7/conf2d, fixed@0.08, poly<0.5 | val | 13 | 5.204 | +1.17% | 40.557 | +8.69% | +69.23% | 6.067 |
| sharpe | pre_drop | strong>=0.7/conf2d, fixed@0.08, poly<0.5 | test | 3 | 4.510 | +0.15% | 78.120 | +0.32% | +66.67% | 10.800 |
| calmar | all | strong>=0.6/conf2d, close_vol_trailing@3, poly<0.55 | train | 242 | 3.747 | +3.52% | 7.378 | +38.79% | +66.53% | 17.035 |
| calmar | all | strong>=0.6/conf2d, close_vol_trailing@3, poly<0.55 | val | 107 | 1.828 | +4.25% | 5.888 | +6.87% | +50.47% | 35.952 |
| calmar | all | strong>=0.6/conf2d, close_vol_trailing@3, poly<0.55 | test | 65 | 0.127 | +4.77% | 0.164 | +0.11% | +43.08% | 46.800 |
| calmar | pre_drop | strong>=0.7/conf2d, close_vol_trailing@3, poly<0.5 | train | 43 | 1.271 | +4.22% | 1.308 | +7.67% | +74.42% | 3.132 |
| calmar | pre_drop | strong>=0.7/conf2d, close_vol_trailing@3, poly<0.5 | val | 15 | 5.154 | +1.15% | 44.822 | +9.32% | +53.33% | 7.000 |
| calmar | pre_drop | strong>=0.7/conf2d, close_vol_trailing@3, poly<0.5 | test | 3 | 4.510 | +0.15% | 78.120 | +0.32% | +66.67% | 10.800 |
| return_maxdd | all | strong>=0.7/conf2d, close_vol_trailing@3, poly<0.5 | train | 227 | 3.794 | +3.52% | 7.624 | +40.14% | +64.76% | 15.979 |
| return_maxdd | all | strong>=0.7/conf2d, close_vol_trailing@3, poly<0.5 | val | 93 | 1.818 | +5.00% | 5.372 | +7.34% | +43.01% | 31.248 |
| return_maxdd | all | strong>=0.7/conf2d, close_vol_trailing@3, poly<0.5 | test | 63 | -0.114 | +3.61% | -0.468 | -0.24% | +39.68% | 45.360 |
| return_maxdd | pre_drop | strong>=0.7/conf2d, close_vol_trailing@3, poly<0.5 | train | 43 | 1.271 | +4.22% | 1.308 | +7.67% | +74.42% | 3.132 |
| return_maxdd | pre_drop | strong>=0.7/conf2d, close_vol_trailing@3, poly<0.5 | val | 15 | 5.154 | +1.15% | 44.822 | +9.32% | +53.33% | 7.000 |
| return_maxdd | pre_drop | strong>=0.7/conf2d, close_vol_trailing@3, poly<0.5 | test | 3 | 4.510 | +0.15% | 78.120 | +0.32% | +66.67% | 10.800 |

## IS vs OOS Gap

| objective | universe | train_sharpe | test_sharpe | train_minus_test |
|---|---|---:|---:|---:|
| calmar | all | 3.747 | 0.127 | 3.620 |
| calmar | pre_drop | 1.271 | 4.510 | -3.239 |
| return_maxdd | all | 3.794 | -0.114 | 3.908 |
| return_maxdd | pre_drop | 1.271 | 4.510 | -3.239 |
| sharpe | all | 3.747 | 0.127 | 3.620 |
| sharpe | pre_drop | 1.307 | 4.510 | -3.203 |

## OOS Baselines

| universe | baseline | trades | sharpe | maxDD | calmar | net | hit | turnover |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| all | equal_weight_alpha_resolution | 52 | -0.226 | +3.22% | -0.773 | -0.35% | +44.23% | 37.440 |
| all | long_only_resolution | 52 | 2.104 | +3.43% | 7.605 | +3.27% | +42.31% | 37.440 |
| all | current_ml_feature_resolution | 52 | 2.406 | +2.55% | 10.112 | +3.23% | +57.69% | 37.440 |
| all | random_alpha_resolution | 52 | 2.123 | +2.18% | 9.413 | +2.62% | +61.54% | 37.440 |
| all | long_energy_proxy_resolution | 8 | -0.616 | +0.79% | -1.667 | -0.18% | +37.50% | 5.929 |
| pre_drop | equal_weight_alpha_resolution | 4 | 4.991 | +0.15% | 69.530 | +1.10% | +75.00% | 3.733 |
| pre_drop | long_only_resolution | 4 | 4.991 | +0.15% | 69.530 | +1.10% | +75.00% | 3.733 |
| pre_drop | current_ml_feature_resolution | 4 | 4.991 | +0.15% | 69.530 | +1.10% | +75.00% | 3.733 |
| pre_drop | random_alpha_resolution | 4 | 2.036 | +0.54% | 8.198 | +0.46% | +50.00% | 3.733 |
| pre_drop | long_energy_proxy_resolution | 0 | 0.000 | +0.00% | 0.000 | +0.00% | +0.00% | 0.000 |

Notes: `long_energy_proxy_resolution` is the closest available proxy for `long-oil-on-escalation` using candidate archetype text and energy hedges. `current_ml_feature_resolution` uses the legacy `feat_ml_dir` direction from the candidate contract.

## Artifacts

- Full train/val sweep plus selected-policy test rows: `data\portfolio_sweep_results.csv`

## B4 Status

The learned RL/differentiable policy is intentionally not started. The task gates B4 behind a v1 floor and a real Model-1 per-trade edge; current `ALPHA_RESULTS.md` marks Model 1 test success as FAIL, so a learned policy would be overfit-prone frontier work.

## Config

```json
{
  "run_id":"339084de-3ab2-4548-b06b-350feff0a376",
  "book":100000.0,
  "position_fraction":0.1,
  "max_positions":10,
  "sector_cap":4,
  "alpha_mode":"parquet",
  "universe":"all",
  "objective":"sharpe",
  "maxdd_cap":0.2,
  "seed":7
}
```
