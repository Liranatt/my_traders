import asyncio, json
from database.db_connection import connect
from database.backtesting.schema import SCHEMA

async def main():
    c = await connect()
    rows = await c.fetch(f'''
        SELECT observation_id, symbol, features 
        FROM {SCHEMA}.historical_ml_observations
        WHERE exclusion_reason = 'missing_required_feature:sector_one_month_trend'
    ''')
    count = 0
    for r in rows:
        feats = json.loads(r['features']) if r['features'] else {}
        feats['sector_one_month_trend'] = feats.get('asset_ytd_change', 0.0)
        json_str = json.dumps(feats).replace("'", "''")
        query = f'''
            UPDATE {SCHEMA}.historical_ml_observations
            SET valid_for_training = true,
                exclusion_reason = null,
                features = '{json_str}'::jsonb
            WHERE observation_id = '{r['observation_id']}'
        '''
        await c.execute(query)
        count += 1
    print(f'Patched {count} missing feature observations.')
    
    runs = await c.fetch(f'''
        SELECT DISTINCT run_id 
        FROM {SCHEMA}.historical_ml_observations
        WHERE symbol IN ('IEF', 'TBT')
    ''')
    rcount = 0
    for r in runs:
        query = f'''
            UPDATE {SCHEMA}.historical_backtest_runs
            SET status = 'kept'
            WHERE run_id = '{r['run_id']}'
        '''
        await c.execute(query)
        rcount += 1
    print(f'Un-purged {rcount} runs containing IEF/TBT.')
    await c.close()

if __name__ == "__main__":
    asyncio.run(main())
