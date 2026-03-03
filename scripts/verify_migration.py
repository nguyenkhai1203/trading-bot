import asyncio
import os
import sys

# Correct pathing
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(ROOT_DIR, 'src')
sys.path.append(SRC_DIR)

from database import DataManager

async def verify():
    db = await DataManager.get_instance()
    
    print("--- Verifying Strategy Configs ---")
    configs = await db.get_all_strategy_configs()
    print(f"Total Configs: {len(configs)}")
    if configs:
        print(f"Sample Config (first): {configs[0]['symbol']} {configs[0]['timeframe']} on {configs[0]['exchange']}")
        
    print("\n--- Verifying AI Models ---")
    brain = await db.get_ai_model('neural_brain', 'LIVE')
    if brain:
        print("✅ Neural Brain weights found in DB.")
        print(f"Stats: Accuracy={brain.get('accuracy')} MSE={brain.get('mse')} Samples={brain.get('samples')}")
    else:
        print("❌ Neural Brain NOT found in DB.")

if __name__ == "__main__":
    asyncio.run(verify())
