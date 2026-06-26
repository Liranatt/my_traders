import asyncio
import pandas as pd
from database.db_connection import connect

async def analyze_markets():
    conn = await connect()
    try:
        # Fetch a large sample of markets
        records = await conn.fetch("""
            SELECT event_id, event_title, market_question 
            FROM checking_relevant_events.historical_market_decisions 
            LIMIT 2000
        """)
        
        df = pd.DataFrame([dict(r) for r in records])
        print(f"Total markets fetched: {len(df)}")
        
        if len(df) == 0:
            print("No markets found in historical_market_decisions. Trying historical_run_markets...")
            records = await conn.fetch("""
                SELECT event_id, question as market_question 
                FROM checking_relevant_events.historical_run_markets 
                LIMIT 2000
            """)
            df = pd.DataFrame([dict(r) for r in records])
            print(f"Total markets fetched: {len(df)}")
            
        if len(df) == 0:
            return
            
        # Basic keyword clustering to see what kinds of questions exist
        def categorize(q):
            q_lower = str(q).lower()
            if 'earnings' in q_lower or 'revenue' in q_lower or 'eps' in q_lower:
                return 'Earnings / Financials'
            elif 'rate' in q_lower or 'fed' in q_lower or 'powell' in q_lower or 'cpi' in q_lower or 'inflation' in q_lower:
                return 'Macroeconomic / Fed'
            elif 'approve' in q_lower or 'fda' in q_lower or 'sec' in q_lower or 'ban' in q_lower or 'court' in q_lower:
                return 'Regulatory / Legal'
            elif 'war' in q_lower or 'strike' in q_lower or 'israel' in q_lower or 'ukraine' in q_lower or 'russia' in q_lower or 'china' in q_lower:
                return 'Geopolitical / Conflict'
            elif 'launch' in q_lower or 'release' in q_lower or 'announce' in q_lower or 'gpt' in q_lower:
                return 'Product Releases / Tech'
            elif 'price' in q_lower or 'hit' in q_lower or 'close' in q_lower:
                return 'Asset Prices / Targets'
            else:
                return 'Other'

        df['category'] = df['market_question'].apply(categorize)
        
        print("\n--- Market Categories ---")
        print(df['category'].value_counts())
        
        print("\n--- Sample Questions per Category ---")
        for cat in df['category'].unique():
            print(f"\n[{cat}]")
            sample = df[df['category'] == cat].sample(min(3, len(df[df['category'] == cat])))
            for _, row in sample.iterrows():
                print(f" - {row.get('event_title', '')} | {row['market_question']}")
                
    except Exception as e:
        print(f"Error: {e}")
    finally:
        await conn.close()

if __name__ == "__main__":
    asyncio.run(analyze_markets())
