import asyncio
import pandas as pd
from database.db_connection import connect
from database.backtesting.schema import SCHEMA
from general_testing.liran_strategy import DEFAULT_POLICY, price_prob_paths, run_backtest

async def analyze():
    print("Loading parquet...")
    df = pd.read_parquet("data/candidates.parquet")
    print(f"Loaded {len(df)} rows.")
    
    # Check for macro events
    macro_kws = ["fed", "rate", "job", "cpi", "inflation", "gdp"]
    macro_markets = df[df["question"].str.lower().str.contains("|".join(macro_kws), na=False)]
    print(f"\nFound {len(macro_markets['market_id'].unique())} macro markets (Fed/Rate/CPI/Jobs) out of {len(df['market_id'].unique())} total markets.")
    
    print("\nSample of macro markets:")
    sample = macro_markets.drop_duplicates(subset=["market_id"])
    for _, row in sample.head(10).iterrows():
        print(f" - {row['question']} -> Mapped Asset: {row['symbol']} (Relevance: {row['feat_connection_strength']:.2f})")

    print("\nRunning fast baseline simulation on a subset to analyze behavior...")
    # Just run on 100 markets to see if it works as intended
    sample_df = df.drop_duplicates(subset=["market_id"]).head(100)
    P, PR = await price_prob_paths(sample_df)
    trades = run_backtest(sample_df, P, PR, DEFAULT_POLICY)
    
    if trades.empty:
        print("No trades triggered in the sample.")
        return
        
    print(f"Simulated {len(trades)} trades from {len(sample_df)} markets.")
    print("\nExit Reasons:")
    print(trades["exit_reason"].value_counts())
    
    print("\nSample of earnings trades (where trailing stop/profit lock are disabled):")
    earnings_trades = trades[trades["archetype"].str.lower().str.contains("earnings", na=False)]
    for _, row in earnings_trades.head(5).iterrows():
        print(f" - {row['symbol']} | Entry: {row['entry_date']} at {row['entry_price']} | Exit: {row['exit_date']} at {row['exit_price']} | Reason: {row['exit_reason']} | Return: {row['return_pct']}%")

if __name__ == "__main__":
    asyncio.run(analyze())
