import os
import sys
import logging
from analyzer import StrategyAnalyzer
from config import TRADING_SYMBOLS

# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("optimizer.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("DailyOptimizer")

def run_optimization():
    logger.info("Starting Daily Strategy Optimization...")
    
    analyzer = StrategyAnalyzer()
    
    # Ensure data directory exists
    if not os.path.exists('data'):
        os.makedirs('data')
        
    updated_count = 0
    
    for symbol in TRADING_SYMBOLS:
        try:
            logger.info(f"Optimizing {symbol}...")
            
            # Run Analysis
            # This loads data, calculates features, checks correlations, and returns weights
            new_weights = analyzer.analyze(symbol)
            
            if new_weights:
                # Count valid weights
                count = len([w for w in new_weights.values() if w > 0])
                
                if count > 0:
                    analyzer.update_config(symbol, new_weights)
                    logger.info(f"✅ Updated {symbol}: {count} active signals found.")
                    updated_count += 1
                else:
                    logger.warning(f"⚠️  {symbol}: No profitable signals found (Safety Mode).")
            else:
                 logger.warning(f"❌ {symbol}: Analysis failed (No Data?).")
                 
        except Exception as e:
            logger.error(f"Error optimizing {symbol}: {e}")
            
    logger.info(f"Optimization Complete. Updated {updated_count}/{len(TRADING_SYMBOLS)} assets.")

if __name__ == "__main__":
    run_optimization()
