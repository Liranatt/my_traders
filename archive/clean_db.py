import asyncio
import shutil
from pathlib import Path
from database.db_connection import connect

async def clean_experiment(exp_id: str):
    conn = await connect()
    try:
        # Delete from the main table (if there's a cascade, this is enough, otherwise we do all)
        # Let's delete from all tables just to be safe
        await conn.execute(f"DELETE FROM checking_relevant_events.historical_asset_selection_experiment_assets WHERE experiment_id='{exp_id}'")
        await conn.execute(f"DELETE FROM checking_relevant_events.historical_asset_selection_experiment_results WHERE experiment_id='{exp_id}'")
        await conn.execute(f"DELETE FROM checking_relevant_events.historical_asset_selection_experiment_queries WHERE experiment_id='{exp_id}'")
        await conn.execute(f"DELETE FROM checking_relevant_events.historical_asset_selection_experiments WHERE experiment_id='{exp_id}'")
        print("Database tables cleaned.")
    finally:
        await conn.close()
    
    # Delete the output folder
    output_dir = Path("main_backtesting/output/asset_selection_experiments") / exp_id
    if output_dir.exists():
        shutil.rmtree(output_dir)
        print("Output directory deleted.")

if __name__ == "__main__":
    asyncio.run(clean_experiment('57c90f19-daac-494c-a578-30321a7edce4'))
