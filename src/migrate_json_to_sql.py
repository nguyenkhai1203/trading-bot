import asyncio
import json
import os
import argparse
import logging
from datetime import datetime

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("Migrator")

try:
    from database import DataManager
except ImportError:
    print("Please run this script from the src directory or ensure python path covers it.")
    exit(1)

def load_json(filepath):
    if not os.path.exists(filepath):
        return None
    with open(filepath, 'r', encoding='utf-8') as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            logger.error(f"Error parsing JSON file: {filepath}")
            return None

async def migrate():
    parser = argparse.ArgumentParser(description="Migrate old JSON data to SQLite DB")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be migrated without inserting")
    parser.add_argument("--backup", action="store_true", help="Backup JSON files before migrating")
    args = parser.parse_args()

    # Paths
    base_dir = os.path.dirname(os.path.abspath(__file__))
    pos_file = os.path.join(base_dir, "positions.json")
    hist_file = os.path.join(base_dir, "signal_performance.json")
    
    if args.backup:
        import shutil
        if os.path.exists(pos_file):
            shutil.copy2(pos_file, pos_file + ".bak")
            logger.info(f"Backed up {pos_file} to .bak")
        if os.path.exists(hist_file):
            shutil.copy2(hist_file, hist_file + ".bak")
            logger.info(f"Backed up {hist_file} to .bak")

    db = await DataManager.get_instance('LIVE')
    
    # Needs a default profile since old system didn't have profiles
    try:
        profile_id = await db.add_profile("Default Migrate Profile", "LIVE", "BINANCE", "Auto-created during migration")
    except Exception as e:
        logger.error(f"Failed to create default profile: {e}")
        return
        
    logger.info(f"Using Profile ID {profile_id} for migrated trades.")

    # 1. Migrate Active Positions
    pos_data = load_json(pos_file)
    migrated_active = 0
    if pos_data and 'active_positions' in pos_data:
        for symbol, pd in pos_data['active_positions'].items():
            trade_obj = {
                'profile_id': profile_id,
                'exchange': 'BINANCE', # Default assumption for old data
                'exchange_order_id': pd.get('id'),
                'symbol': symbol,
                'side': pd.get('side', 'BUY').upper(),
                'qty': pd.get('amount', 0),
                'entry_price': pd.get('entry_price', 0),
                'sl_price': pd.get('sl'),
                'tp_price': pd.get('tp'),
                'status': 'ACTIVE',
                'timeframe': pd.get('timeframe'),
                'entry_time': int(pd.get('timestamp', datetime.now().timestamp()*1000)),
                'meta': {'old_json_data': True}
            }
            if args.dry_run:
                logger.info(f"[DRY-RUN] Would insert active trade: {symbol} ID: {trade_obj['exchange_order_id']}")
            else:
                try:
                    await db.save_position(trade_obj)
                    migrated_active += 1
                except Exception as e:
                    logger.error(f"Failed to migrate active trade {symbol}: {e}")
                    
    logger.info(f"Migrated {migrated_active} active positions.")

    # 2. Migrate Trade History
    hist_data = load_json(hist_file)
    migrated_hist = 0
    if hist_data and 'trades' in hist_data:
        for t in hist_data['trades']:
            trade_obj = {
                'profile_id': profile_id,
                'exchange': 'BINANCE',
                'symbol': t.get('symbol', 'UNKNOWN'),
                'side': t.get('side', 'BUY').upper(),
                'qty': 0, # Old history typically didn't store qty
                'entry_price': t.get('entry_price'),
                'exit_price': t.get('exit_price'),
                'status': 'CLOSED',
                'pnl': t.get('pnl', 0),
                'exit_reason': 'migrated_history',
                'meta': t
            }
            if args.dry_run:
                logger.info(f"[DRY-RUN] Would insert history trade: {trade_obj['symbol']} PNL: {trade_obj['pnl']}")
            else:
                try:
                    await db.insert_trade_history(trade_obj)
                    migrated_hist += 1
                except Exception as e:
                    logger.error(f"Failed to migrate history trade {t}: {e}")
                    
    logger.info(f"Migrated {migrated_hist} historical trades.")
    
    if args.dry_run:
        logger.info("DRY RUN FINISHED. No data was actually written to database.")
    else:
        logger.info("Migration to SQLite completed successfully!")

if __name__ == "__main__":
    if os.name == 'nt':
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(migrate())
