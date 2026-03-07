import json
import os
import sqlite3
import time
import sys
import asyncio

# Correct pathing
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(ROOT_DIR, 'src')
sys.path.append(SRC_DIR)

from database import DataManager

async def migrate_strategy_configs():
    config_path = os.path.join(SRC_DIR, 'strategy_config.json')
    if not os.path.exists(config_path):
        print(f"⚠️ Skip Strategy Config: {config_path} not found.")
        return
        
    print(f"📄 Loading Strategy Config from {config_path}...")
    with open(config_path, 'r') as f:
        data = json.load(f)
        
    db = await DataManager.get_instance()
    count = 0
    
    for key, config in data.items():
        if key == 'default':
            # Store default as a special record
            await db.save_strategy_config('default', 'default', 'default', config)
            count += 1
            continue
            
        # Standard Key Format: EXCHANGE_SYMBOL_TF (e.g. BINANCE_BTC_USDT_1h)
        # Or SYMBOL_TF (e.g. BTC_USDT_1h)
        parts = key.split('_')
        
        exchange = 'BINANCE' # Default fallback
        symbol = None
        timeframe = None
        
        # Heuristic parsing for various key formats
        if parts[0] in ['BINANCE', 'BYBIT']:
            exchange = parts[0]
            # Handle cases like BINANCE_BTC_USDT_1H
            if len(parts) >= 4:
                symbol = f"{parts[1]}/{parts[2]}"
                timeframe = parts[3]
            elif len(parts) == 3:
                symbol = parts[1]
                timeframe = parts[2]
        else:
            # Handle cases like BTC_USDT_1H
            if len(parts) >= 3:
                symbol = f"{parts[0]}/{parts[1]}"
                timeframe = parts[2]
            elif len(parts) == 2:
                symbol = parts[0]
                timeframe = parts[1]
        
        if symbol and timeframe:
            await db.save_strategy_config(symbol, timeframe, exchange, config)
            count += 1
            
    print(f"✅ Migrated {count} strategy configurations to database.")

async def migrate_ai_models():
    weights_path = os.path.join(SRC_DIR, 'brain_weights.json')
    if not os.path.exists(weights_path):
        print(f"⚠️ Skip AI Models: {weights_path} not found.")
        return
        
    print(f"📄 Loading AI Weights from {weights_path}...")
    with open(weights_path, 'r') as f:
        data = json.load(f)
        
    db = await DataManager.get_instance()
    weights_json = json.dumps(data)
    
    # Save as 'neural_brain' for 'LIVE' environment
    await db.save_ai_model('neural_brain', 'LIVE', weights_json, 0, 0, 0)
    print(f"✅ Migrated Neural Brain weights to database.")

async def main():
    print("🚀 Starting Unified Migration (JSON -> DB)...")
    await migrate_strategy_configs()
    await migrate_ai_models()
    print("🏁 Migration complete.")

if __name__ == "__main__":
    asyncio.run(main())
