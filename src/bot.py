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
    def __init__(self, symbol, timeframe, data_manager, trader):
        self.symbol = symbol
        self.timeframe = timeframe
        self.data_manager = data_manager 
        self.feature_engineer = FeatureEngineer()
        self.strategy = WeightedScoringStrategy(symbol=symbol, timeframe=timeframe) 
        # Weights are loaded automatically in __init__ now
        self.risk_manager = RiskManager(risk_per_trade=RISK_PER_TRADE, leverage=LEVERAGE)
        self.trader = trader
        self.logger = logging.getLogger(__name__)
        self.running = True
        
    def get_tier_config(self, score):
        """Get sizing tier based on confidence score."""
        return self.strategy.get_sizing_tier(score)

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

            # Ensure current_price is available for all checks
            current_price = df.iloc[-1]['close']

            # Check if we already have a position for this symbol/timeframe
            pos_key = f"{self.symbol}_{self.timeframe}"
            existing_pos = self.trader.active_positions.get(pos_key)
            if existing_pos:
                sl = existing_pos.get('sl')
                tp = existing_pos.get('tp')
                side = existing_pos.get('side')
                
                print(f"📍 [{self.symbol}] Monitoring: {side} | Price: {current_price:.3f} | SL: {sl:.3f} | TP: {tp:.3f}")
                
                # 1. Check for Exit Conditions (SL/TP)
                exit_hit = False
                exit_reason = ""
                
                if side == 'BUY':
                    if sl and current_price <= sl:
                        exit_hit = True
                        exit_reason = f"STOP LOSS hit at {current_price}"
                    elif tp and current_price >= tp:
                        exit_hit = True
                        exit_reason = f"TAKE PROFIT hit at {current_price}"
                elif side == 'SELL':
                    if sl and current_price >= sl:
                        exit_hit = True
                        exit_reason = f"STOP LOSS hit at {current_price}"
                    elif tp and current_price <= tp:
                        exit_hit = True
                        exit_reason = f"TAKE PROFIT hit at {current_price}"

                if exit_hit:
                    mode_label = "✅ REAL RUNNING" if not self.trader.dry_run else "🧪 TEST"
                    msg = f"{mode_label} CLOSED: [{self.symbol}] {exit_reason}"
                    print(msg)
                    await send_telegram_message(msg)
                    await self.trader.remove_position(self.symbol, timeframe=self.timeframe, exit_price=current_price, exit_reason=exit_reason)
                    # Don't return, allow it to check for new signals in the same step if desired,
                    # but usually better to wait for next heartbeat. 
                    return 

                # 2. In Live mode, sync with exchange
                if not self.trader.dry_run:
                    # Double check if still open on exchange 
                    still_open = await self.trader.get_open_position(self.symbol, timeframe=self.timeframe)
                    if not still_open:
                        await self.trader.remove_position(self.symbol, timeframe=self.timeframe, exit_price=current_price, exit_reason="EXTERNAL_EXIT")
                        return

                # If position is still open and no exit hit, skip entry analysis
                return

            # GLOBAL GUARD: Protected by per-symbol lock to prevent race conditions
            async with self.trader._get_lock(self.symbol):
                already_in_symbol = await self.trader.has_any_symbol_position(self.symbol)
                if already_in_symbol:
                    # Silently skip entry to avoid overlapping positions on same coin
                    return

                df = self.feature_engineer.calculate_features(df)
                last_row = df.iloc[-1]
                # current_price already defined at top

                signal_data = self.strategy.get_signal(last_row)
                side = signal_data['side']
                conf = signal_data['confidence']

                if side in ['BUY', 'SELL']:
                    self.logger.info(f"[{self.symbol} {self.timeframe}] Signal: {side} ({conf})")
                    print(f"🎯 [{self.symbol} {self.timeframe}] SIGNAL FOUND: {side} | Conf: {conf:.2f} | Price: {current_price:.3f}")
                    
                    # Use dynamic SL/TP from strategy (optimized by analyzer)
                    sl, tp = self.risk_manager.calculate_sl_tp(
                        current_price, side, 
                        sl_pct=self.strategy.sl_pct, 
                        tp_pct=self.strategy.tp_pct
                    )
                    
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
                        res = await self.trader.place_order(self.symbol, exec_side, qty, timeframe=self.timeframe, price=current_price, sl=sl, tp=tp)
                        if res:
                            mode_label = "✅ REAL RUNNING" if not self.trader.dry_run else "🧪 TEST"
                            msg = (
                                f"{mode_label} 🚀\n"
                                f"**[{self.symbol}] {side}**\n"
                                f"Qty: {qty:.3f}\n"
                                f"Entry: {current_price:.3f}\n"
                                f"SL: {sl:.3f} | TP: {tp:.3f}\n"
                                f"Score: {score:.1f} | Lev: {target_lev}x\n"
                                f"Margin: {risk_info}"
                            )
                            print(msg)
                            await send_telegram_message(msg)


        except Exception as e:
            self.logger.error(f"Error in bot step {self.symbol}: {e}")

import time
from analyzer import run_global_optimization
from notification import send_telegram_message, send_telegram_chunked

async def send_periodic_status_report(trader):
    """Aggregates all active positions and sends a summary to Telegram."""
    positions = trader.active_positions
    if not positions:
        # await send_telegram_message("📊 **Status Update**: No open positions.")
        return

    msg = "📊 **Active Positions Summary** 📊\n\n"
    for key, pos in positions.items():
        symbol = pos.get('symbol', key.split('_')[0])
        side = pos.get('side', 'N/A').upper()
        entry = pos['entry_price']
        qty = pos['qty']
        
        # PnL Calculation (Approximate using entry vs current if we had current, 
        # but for simplicity we report entry and size)
        msg += (
            f"**{symbol}** ({side})\n"
            f"Size: {qty:.3f}\n"
            f"Entry: {entry:.3f}\n"
            f"-------------------\n"
        )
    
    await send_telegram_chunked(msg)

async def main():
    manager = MarketDataManager()
    bots = []
    
    print("Initializing Bots...")
    # 0. Shared Trader (One instance for all bots to sync positions)
    trader = Trader(manager.exchange, dry_run=True)

    # Initialize one bot per pair/tf
    for symbol in TRADING_SYMBOLS:
        for tf in TRADING_TIMEFRAMES:
            bot = TradingBot(symbol, tf, manager, trader)
            bots.append(bot)
            
    # Initial Notif
    await send_telegram_message(f"🤖 Bot Started! Monitoring {len(TRADING_SYMBOLS)} symbols.")
    
    # Track optimization time (set to 0 to trigger first run if needed)
    last_auto_opt = time.time()
    opt_interval = 12 * 3600 # 12 hours

    # Track periodic status update
    last_status_update = time.time()
    status_interval = 2 * 3600 # 2 hours
            
    print("Starting Loop...")
    try:
        initial_balance = 1000 # Mock or fetch once
        
        while True:
            # 0. Check for Auto-Optimization (Twice a day)
            curr_time = time.time()
            if curr_time - last_auto_opt >= opt_interval:
                print("⏰ Scheduled Auto-Optimization triggered...")
                try:
                    await run_global_optimization()
                    last_auto_opt = curr_time
                    # Reload config for all bots
                    for bot in bots:
                        bot.strategy.reload_config()
                    await send_telegram_message("🔄 Auto-Optimization Complete and Bot Configs Reloaded.")
                except Exception as opt_err:
                    print(f"Error during auto-optimization: {opt_err}")

            # 0.1 Check for Periodic Status Update (Every 2 hours)
            if curr_time - last_status_update >= status_interval:
                print("📊 Sending periodic status update...")
                try:
                    await send_periodic_status_report(trader)
                    last_status_update = curr_time
                except Exception as status_err:
                    print(f"Error sending status update: {status_err}")

            # 1. Centralized Data Fetch
            print(f"🔄 Heartbeat: Updating data for {len(TRADING_SYMBOLS)} symbols...")
            await manager.update_data(TRADING_SYMBOLS, TRADING_TIMEFRAMES)
            
            # 2. Run Logic for all bots
            tasks = [bot.run_step(initial_balance) for bot in bots if bot.running]
            if tasks:
                await asyncio.gather(*tasks)
            
            await asyncio.sleep(60) # 60s interval - SL/TP is set on entry, no need for frequent polling
            
    except KeyboardInterrupt:
        print("Stopping...")
    finally:
        await manager.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
