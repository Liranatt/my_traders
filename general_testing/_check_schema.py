import asyncio
from database.db_connection import connect
from database.backtesting.schema import SCHEMA

async def f():
    c = await connect()
    try:
        res = await c.fetch(f'SELECT * FROM {SCHEMA}.historical_price_bars LIMIT 1')
        print(list(res[0].keys()))
    finally:
        await c.close()

asyncio.run(f())
