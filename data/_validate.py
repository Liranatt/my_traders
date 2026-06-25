"""Quick validation of candidates.parquet."""
import pandas as pd
import numpy as np
from scipy.stats import spearmanr

df = pd.read_parquet('data/candidates.parquet')

# Check contract columns
contract = ['run_id','event_id','market_id','symbol','pass_number','t0','t_theta','t_e',
            'entry_price','sector_etf','y_hedged','realized_dir','realized_abs_move',
            'alpha_score','alpha_dir','split']
missing = [c for c in contract if c not in df.columns]
print('Missing contract cols:', missing)

# Check feature columns from task
feat_expected = ['feat_prob_at_trigger','feat_prob_slope_24h','feat_prob_volatility',
                 'feat_prob_surge_since_t0','feat_time_to_resolution_days',
                 'feat_crossing_latency_days','feat_pre_entry_volume_log',
                 'feat_connection_strength','feat_archetype','feat_world_size','feat_asset_role',
                 'feat_runup_since_t0','feat_asset_2w_trend','feat_sector_1m_trend',
                 'feat_spy_2w_trend','feat_ytd_change',
                 'feat_debt_to_equity','feat_cash_to_marketcap','feat_beta','feat_profit_margin',
                 'feat_log_market_cap','feat_sector',
                 'feat_runup_rank','feat_size_rank',
                 'feat_ml_class_prob','feat_ml_pred_peak','feat_ml_dir','feat_momentum_roc']
missing_feat = [f for f in feat_expected if f not in df.columns]
print('Missing feature cols:', missing_feat)

# Check pre_drop_earnings_defense
print('Has pre_drop_earnings_defense:', 'pre_drop_earnings_defense' in df.columns)
print('pre_drop values:', df['pre_drop_earnings_defense'].value_counts().to_dict())

# Check median |y_hedged| is in 3-6% range
abs_med = df['y_hedged'].abs().median()
print(f'Median |y_hedged|: {abs_med*100:.2f}% (spec: 3-6%)')

# Check realized_dir = sign(y_hedged)
sign_match = (df['realized_dir'] == np.sign(df['y_hedged']).astype(int)).mean()
print(f'realized_dir matches sign(y_hedged): {sign_match:.2%}')

# Check alpha_dir consistency
alpha_dir_match = ((df['alpha_dir'] == 'long') == (df['alpha_score'] >= 0)).mean()
print(f'alpha_dir matches alpha_score sign: {alpha_dir_match:.2%}')

# Check split logic
train = df[df['split']=='train']
val = df[df['split']=='val']
test = df[df['split']=='test']
embargo = df[df['split']=='embargo']
print(f'Train t_theta range: {train["t_theta"].min()} to {train["t_theta"].max()}')
print(f'Val t_theta range: {val["t_theta"].min()} to {val["t_theta"].max()}')
print(f'Test t_theta range: {test["t_theta"].min()} to {test["t_theta"].max()}')
print(f'Embargo t_theta range: {embargo["t_theta"].min()} to {embargo["t_theta"].max()}')

# Check OOS model performance
for s_name, s_df in [('train', train), ('val', val), ('test', test)]:
    if len(s_df) > 2:
        corr, pval = spearmanr(s_df['alpha_score'], s_df['y_hedged'])
        top_q = s_df.nlargest(max(1, len(s_df)//5), 'alpha_score')['y_hedged'].mean()
        bot_q = s_df.nsmallest(max(1, len(s_df)//5), 'alpha_score')['y_hedged'].mean()
        print(f'{s_name}: n={len(s_df)}, rank_corr={corr:.4f} (p={pval:.4f}), '
              f'top-quintile mean y_hedged={top_q*100:.2f}%, '
              f'bot-quintile mean y_hedged={bot_q*100:.2f}%, '
              f'spread={((top_q - bot_q)*100):.2f}%')

# Fundamentals imputation check
fund_cols = ['feat_debt_to_equity','feat_cash_to_marketcap','feat_beta','feat_profit_margin','feat_log_market_cap']
for c in fund_cols:
    nunique = df[c].nunique()
    print(f'  {c}: nunique={nunique}, median={df[c].median():.4f}')

# Check if model is just predicting noise
print(f'\nalpha_score std: {df["alpha_score"].std():.4f}')
print(f'y_hedged std: {df["y_hedged"].std():.4f}')
print(f'alpha_score/y_hedged std ratio: {df["alpha_score"].std()/df["y_hedged"].std():.4f}')
