"""Quick audit script to check for lookahead bias in candidates.parquet."""
import pandas as pd

df = pd.read_parquet("data/candidates.parquet")

# 1. Date ranges per split — check they are chronologically ordered
print("=== Date Ranges Per Split ===")
for s in ["train", "val", "test"]:
    sub = df[df.split == s]
    print(f"  {s}: t_theta {sub.t_theta.min()} -> {sub.t_theta.max()}")
    print(f"  {s}: t_e     {sub.t_e.min()} -> {sub.t_e.max()}")

# 2. Check for overlap: does any test t_theta fall within the train date range?
train_max = df[df.split == "train"]["t_theta"].max()
val_min = df[df.split == "val"]["t_theta"].min()
val_max = df[df.split == "val"]["t_theta"].max()
test_min = df[df.split == "test"]["t_theta"].min()
print(f"\n  train max t_theta: {train_max}")
print(f"  val   min t_theta: {val_min}")
print(f"  val   max t_theta: {val_max}")
print(f"  test  min t_theta: {test_min}")
print(f"  LEAK? train overlaps val: {train_max >= val_min}")
print(f"  LEAK? val overlaps test:  {val_max >= test_min}")

# 3. Check if asset_return is computed from entry_price / exit_price  (i.e. known at t_theta?)
print("\n=== asset_return vs computed return ===")
df["computed_return"] = df["exit_price"] / df["entry_price"] - 1.0
diff = (df["asset_return"] - df["computed_return"]).abs()
print(f"  max diff: {diff.max():.6f}   mean diff: {diff.mean():.6f}")
print(f"  asset_return IS the exit_price/entry_price ratio: {diff.max() < 0.001}")

# 4. Check what feat_prob_at_trigger represents — is it known at t_theta or after?
print("\n=== Polymarket probability features ===")
print(f"  feat_prob_at_trigger: unique values sample = {df['feat_prob_at_trigger'].dropna().head(20).tolist()}")
print(f"  feat_prob_slope_24h:  range [{df['feat_prob_slope_24h'].min():.4f}, {df['feat_prob_slope_24h'].max():.4f}]")
print(f"  feat_prob_volatility: range [{df['feat_prob_volatility'].min():.4f}, {df['feat_prob_volatility'].max():.4f}]")
print(f"  feat_prob_surge_since_t0: range [{df['feat_prob_surge_since_t0'].min():.4f}, {df['feat_prob_surge_since_t0'].max():.4f}]")

# 5. Check the RF target column in the parquet (it shouldn't exist pre-model)
print(f"\n=== rf_target in parquet? ===")
print(f"  'rf_target' column exists in parquet: {'rf_target' in df.columns}")

# 6. The probability query — check if it uses last prob of the day (end-of-day)
# This is in the SQL: ORDER BY ... hour_ts DESC => DISTINCT ON picks the LAST hour_ts per day
print("\n=== Probability Query Analysis ===")
print("  The SQL uses DISTINCT ON + ORDER BY hour_ts DESC")
print("  This means it picks the LAST probability reading of each day")
print("  -> If hour_ts includes evening/night hours, this could include post-market info")
