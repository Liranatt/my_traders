import asyncio
from database.db_connection import connect
from general_testing.diffusion_research_helpers import load_observations

async def main():
    obs, meta = await load_observations(statuses=['complete', 'kept'], dedupe=True)
    for o in obs:
        if o['symbol'] in ('SHY', 'IEF', 'TBT'):
            print(f"Symbol: {o['symbol']}, original_archetype: {o['event_archetype']}, feat_archetype: {o.get('feat_archetype')}, long_eligible: {o.get('long_eligible')}")
            break
asyncio.run(main())
