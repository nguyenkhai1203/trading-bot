import asyncio
import logging
import os
import sys

# Add src to path if running directly
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from config import (
    BYBIT_API_KEY, 
    TRADING_SYMBOLS, 
    TRADING_TIMEFRAMES, 
    LEVERAGE, 
    RISK_PER_TRADE
)
from data_manager import MarketDataManager
from feature_engineering import FeatureEngineer
from strategy import WeightedScoringStrategy 
from risk_manager import RiskManager
from execution import Trader
from notification import send_telegram_message

# ...

class TradingBot:
    def __init__(self, symbol, timeframe, data_manager, dry_run=True):
        self.symbol = symbol
        self.timeframe = timeframe
        self.data_manager = data_manager 
        self.feature_engineer = FeatureEngineer()
        self.strategy = WeightedScoringStrategy(symbol=symbol) # Changed
        # Reload strategy weights on init to be safe
        self.strategy.weights = self.strategy.load_weights(symbol)
        self.risk_manager = RiskManager(risk_per_trade=RISK_PER_TRADE, leverage=LEVERAGE)
        self.trader = Trader(self.data_manager.exchange, dry_run=dry_run)
        
    def get_tier_config(self, score):
        # Load latest config structure
        # In real prod, cache this. For now, access strategy's loaded config if possible or re-read
        # Simplified: Use Strategy's internal method or accessing the file directly is slow. 
        # Better: Strategy stores the full config for the symbol.
        return self.strategy.get_sizing_tier(score)
        self.logger = logging.getLogger(__name__)
        self.running = True

    async def run_step(self, initial_balance):
        # ... (circuit breaker check same) ...
        try:
            stop_trading, reason = self.risk_manager.check_circuit_breaker(initial_balance)
            if stop_trading:
                if self.running: 
                    await send_telegram_message(f"🚨 [{self.symbol}] CIRCUIT BREAKER: {reason}")
                self.running = False
                return

            df = self.data_manager.get_data(self.symbol, self.timeframe)
            if df is None or df.empty:
                return

            df = self.feature_engineer.calculate_features(df)
            last_row = df.iloc[-1]
            current_price = last_row['close']

            signal_data = self.strategy.get_signal(last_row)
            side = signal_data['side']
            conf = signal_data['confidence']

            if side in ['BUY', 'SELL']:
                 self.logger.info(f"[{self.symbol} {self.timeframe}] Signal: {side} ({conf})")
                 
                 sl, tp = self.risk_manager.calculate_sl_tp(current_price, side)
                 
            if side in ['BUY', 'SELL']:
                 self.logger.info(f"[{self.symbol} {self.timeframe}] Signal: {side} ({conf})")
                 
                 sl, tp = self.risk_manager.calculate_sl_tp(current_price, side)
                 
                 # Dynamic Tier Sizing
                 # Score is roughly conf * 10
                 score = conf * 10
                 tier = self.get_tier_config(score)
                 
                 target_lev = tier.get('leverage', LEVERAGE)
                 # Check for cost_usdt (fixed margin) or risk_pct (account %)
                 target_cost = tier.get('cost_usdt', None)
                 target_risk = tier.get('risk_pct', None)
                 
                 qty = 0
                 if target_cost is not None:
                     # Fixed Margin Mode (User Preferred)
                     qty = self.risk_manager.calculate_size_by_cost(current_price, target_cost, target_lev)
                     risk_info = f"${target_cost}"
                 else:
                     # Fallback to Risk %
                     use_risk = target_risk if target_risk else 0.01
                     qty = self.risk_manager.calculate_position_size(
                         initial_balance, current_price, sl, 
                         leverage=target_lev, risk_pct=use_risk
                     )
                     risk_info = f"{use_risk*100}%"
                 
                 if qty > 0:
                     exec_side = side.lower()
                     res = await self.trader.place_order(self.symbol, exec_side, qty, sl=sl, tp=tp)
                     if res:
                         await send_telegram_message(f"🚀 [{self.symbol}] {side} {qty}\nPrice: {current_price}\nScore: {score:.1f}\nLev: {target_lev}x | Margin: {risk_info}")


        except Exception as e:
            self.logger.error(f"Error in bot step {self.symbol}: {e}")

async def main():
    manager = MarketDataManager()
    bots = []
    
    print("Initializing Bots...")
    # Initialize one bot per pair/tf
    for symbol in TRADING_SYMBOLS:
        for tf in TRADING_TIMEFRAMES:
            bot = TradingBot(symbol, tf, manager, dry_run=True)
            bots.append(bot)
            
    # Initial Notif
    await send_telegram_message(f"🤖 Bot Started! Monitoring {len(TRADING_SYMBOLS)} symbols.")
            
    print("Starting Loop...")
    try:
        initial_balance = 1000 # Mock or fetch once
        
        while True:
            # 1. Centralized Data Fetch
            # print("Updating Data...")
            await manager.update_data(TRADING_SYMBOLS, TRADING_TIMEFRAMES)
            
            # 2. Run Logic for all bots
            # Can be done in parallel logic since data is ready
            tasks = [bot.run_step(initial_balance) for bot in bots if bot.running]
            if tasks:
                await asyncio.gather(*tasks)
            
            await asyncio.sleep(5) # Poll interval
            
    except KeyboardInterrupt:
        print("Stopping...")
    finally:
        await manager.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
