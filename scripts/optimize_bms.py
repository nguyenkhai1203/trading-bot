import asyncio
import logging
import sys
import os

# Add src to path
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))

from btc_analyzer import BTCAnalyzer
from data_manager import MarketDataManager
from database import DataManager
import config

async def main():
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("BMS_Optimizer")
    
    logger.info("🚀 Starting BMS Internal Optimization (Loop A)")
    
    db = await DataManager.get_instance('LIVE')
    
    dm = MarketDataManager()
    # Step 0: Ensure BTC data is fresh
    logger.info("Downloading fresh BTC data...")
    await dm.update_data(['BTC/USDT:USDT'], ['1h'], force=True)
    
    analyzer = BTCAnalyzer(dm, db)
    
    # Run Loop A Optimization
    best_weights = analyzer.optimize_weights()
    
    logger.info("✅ BMS Internal Optimization Complete")
    logger.info(f"Optimal Sub-Weights: {best_weights}")
    
    # In a real setup, we might want to save these weights to a config file
    # For now they are set in the analyzer instance
    
    await db.close()

if __name__ == "__main__":
    asyncio.run(main())
