import asyncio
import os
import sys
import time
import logging
from datetime import datetime, timedelta

# Add src to path
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'src'))

import config
from src.infrastructure.repository.database import DataManager
from src.infrastructure.adapters.exchange_factory import create_adapter_from_profile

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("SyncHistory")

async def sync_profile_history(db, profile, lookback_hours=24):
    """Sync history for a specific profile."""
    profile_name = profile['name']
    profile_id = profile['id']
    exchange_name = profile['exchange']
    
    logger.info(f"🔄 [{profile_name}] Starting 24h history sync...")
    
    # 1. Initialize Adapter
    adapter = await create_adapter_from_profile(profile)
    if not adapter:
        logger.error(f"❌ [{profile_name}] Failed to create adapter.")
        return
    
    try:
        await adapter.sync_time()
        await adapter.load_markets()
    except Exception as e:
        logger.error(f"❌ [{profile_name}] Connection failed: {e}")
        await adapter.close()
        return

    # ... remaining code ...
    try:
        # 2. Get Trading Symbols from Config
        config_symbols = config.BINANCE_SYMBOLS if exchange_name == 'BINANCE' else config.BYBIT_SYMBOLS
        config_symbols = [s for s in config_symbols if s in config.TRADING_SYMBOLS]
        
        since = int((datetime.now() - timedelta(hours=lookback_hours)).timestamp() * 1000)
        
        # 3. Fetch all active positions in DB to check for closures
        db_active = await db.get_active_positions(profile_id)
        active_map = {f"{trade['symbol']}_{trade['side']}": trade for trade in db_active}
        
        # 4. Combine Config Symbols + Symbols from Active Positions in DB
        # This ensures we scan symbols the bot *actually* has open, even if config changed.
        final_symbols = set(config_symbols)
        for trade in db_active:
            final_symbols.add(trade['symbol'])
        
        total_fixed = 0
        
        for symbol in final_symbols:
            try:
                logger.info(f"  - Checking {symbol}...")
                # Fetch trades from exchange
                trades = await adapter.fetch_my_trades(symbol, since=since)
                if not trades:
                    continue
                
                # Sort by time
                trades.sort(key=lambda x: x['timestamp'])
                
                for t in trades:
                    # (logic is same)
                    order_id = str(t.get('order') or t.get('orderId') or t.get('id'))
                    side = t['side'].upper() # 'buy' or 'sell'
                    price = float(t['price'])
                    qty = float(t['amount'])
                    ts = t['timestamp']
                    
                    opp_side = 'SELL' if side == 'BUY' else 'BUY'
                    key = f"{symbol}_{opp_side}"
                    
                    if key in active_map:
                        active_trade = active_map[key]
                        if ts > (active_trade.get('entry_time') or 0):
                            logger.info(f"    ✅ Found EXIT for {symbol} {opp_side}: Price {price} at {datetime.fromtimestamp(ts/1000)}")
                            entry_price = float(active_trade['entry_price'])
                            trade_qty = float(active_trade['qty'])
                            pnl_qty = min(qty, trade_qty)
                            if opp_side == 'BUY':
                                pnl = (price - entry_price) * pnl_qty
                            else:
                                pnl = (entry_price - price) * pnl_qty
                            
                            await db.update_position_status(
                                active_trade['id'], 
                                'CLOSED', 
                                exit_price=price, 
                                pnl=pnl, 
                                exit_reason="SYNC(History Script)"
                            )
                            total_fixed += 1
                            del active_map[key]
                            
            except Exception as e:
                logger.error(f"    ❌ Error syncing {symbol}: {e}")
                
        logger.info(f"✨ [{profile_name}] Sync completed. Fixed {total_fixed} positions.")
    finally:
        await adapter.close()

async def main():
    # Environment priority: determine from launcher or default to LIVE
    env = os.getenv('TRADING_ENV', 'LIVE')
    db = await DataManager.get_instance(env)
    
    profiles = await db.get_profiles()
    if not profiles:
        logger.error("No active profiles found in database.")
        return
    
    tasks = [sync_profile_history(db, p) for p in profiles]
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    asyncio.run(main())
