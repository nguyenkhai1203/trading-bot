import asyncio
import os
import sys

# Add src to path
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))

from analyzer import StrategyAnalyzer
import config

async def main():
    print("🚀 Running Trial Global Optimization (Loop B)")
    analyzer = StrategyAnalyzer()
    
    symbol = 'ETH/USDT:USDT'
    tf = '1h'
    exchange = 'BINANCE'
    
    # Run analysis for ETH
    weights = analyzer.analyze(symbol, tf, exchange=exchange)
    print(f"Base Weights: {weights}")
    
    # Run Validation (This includes BMS weight optimization)
    df = analyzer.get_features(symbol, tf, exchange=exchange)
    result = analyzer.validate_weights(df, weights, symbol, tf, exchange=exchange)
    
    if result:
        print(f"✅ Optimization Successful for {symbol}")
        print(f"Optimal w_btc: {result.get('w_btc')}")
        print(f"Consistency: {result.get('consistency')}")
        print(f"PnL: {result.get('pnl')}")
        
        # Verify it can be saved
        analyzer.update_config(symbol, tf, weights, 
                               sl_pct=result['sl_pct'], 
                               tp_pct=result['tp_pct'], 
                               entry_score=result['entry_score'],
                               w_btc=result.get('w_btc'),
                               exchange=exchange)
        print("✅ Config updated with optimized BMS weight")
    else:
        print("❌ Optimization failed to find profitable combo")

if __name__ == "__main__":
    asyncio.run(main())
