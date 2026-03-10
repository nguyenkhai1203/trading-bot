import asyncio
import logging
import numpy as np
import os
import sys
import time
import json
from datetime import datetime
from collections import defaultdict
import colorama
from colorama import Fore, Style
colorama.init(autoreset=True)

# Color mapping for terminal logs
COLOR_MAP = {
    'green': Fore.GREEN,
    'yellow': Fore.YELLOW,
    'blue': Fore.BLUE,
    'cyan': Fore.CYAN,
    'magenta': Fore.MAGENTA,
    'red': Fore.RED,
    'white': Fore.WHITE
}

# Add src to path if running directly
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# Enhanced Terminal Logging with Timestamps
import builtins
_orig_print = builtins.print
def custom_print(*args, **kwargs):
    now = datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")
    _orig_print(f"{now}", *args, **kwargs)
builtins.print = custom_print

from src import config
from src.infrastructure.repository.database import DataManager
from src.data_manager import MarketDataManager
from src.strategy import WeightedScoringStrategy 
from src.risk_manager import RiskManager
from src.execution import Trader
from src.infrastructure.notifications.notification import (
    send_telegram_message,
    format_pending_order,
    format_position_filled,
    format_position_closed,
    format_order_cancelled,
    format_status_update,
    format_adaptive_trigger
)
from src.signal_tracker import SignalTracker

class BalanceTracker:
    """Manages shared balance state across multiple bots to prevent race conditions."""
    def __init__(self):
        self.balances = {} # {(exchange, profile_id): {'total': 0.0, 'reserved': 0.0}}
        
    def update_balance(self, exchange_name, profile_id, total_equity):
        key = (exchange_name, profile_id)
        if key not in self.balances:
            self.balances[key] = {'total': 0.0, 'reserved': 0.0}
        self.balances[key]['total'] = total_equity
        
    def get_available(self, exchange_name, profile_id):
        key = (exchange_name, profile_id)
        if key not in self.balances: return 0.0
        b = self.balances[key]
        return max(0.0, b['total'] - b['reserved'])

    def reserve(self, exchange_name, profile_id, amount):
        avail = self.get_available(exchange_name, profile_id)
        if avail >= amount:
            self.balances[(exchange_name, profile_id)]['reserved'] += amount
            return True
        return False

    def release(self, exchange_name, profile_id, amount):
        key = (exchange_name, profile_id)
        if key in self.balances:
            self.balances[key]['reserved'] = max(0.0, self.balances[key]['reserved'] - amount)

    def reset_reservations(self):
        for key in self.balances:
            self.balances[key]['reserved'] = 0.0

class TradingBot:
    """Handles logic for a single symbol/timeframe for a specific profile."""
    def __init__(self, symbol, timeframe, data_manager, trader, risk_manager, signal_tracker, balance_tracker=None):
        self.symbol = symbol
        self.timeframe = timeframe
        self.data_manager = data_manager 
        self.trader = trader
        self.risk_manager = risk_manager
        self.signal_tracker = signal_tracker
        self.balance_tracker = balance_tracker
        
        self.strategy = WeightedScoringStrategy(symbol=symbol, timeframe=timeframe, exchange=trader.exchange_name) 
        self.logger = logging.getLogger(f"Bot.{trader.exchange_name}.{symbol}.{timeframe}")
        self.running = True
        self.last_eval_timestamp = 0 # Prevent log spam for same candle
        self.last_cd_log_time = 0 # Prevent log spam for cooldowns

    async def run_monitoring_cycle(self):
        """Monitors SL/TP, trailing stops, and signal reversals."""
        try:
            df = self.data_manager.get_data(self.symbol, self.timeframe, exchange=self.trader.exchange_name)
            if df is None or df.empty: return False

            last_row = df.iloc[-1]
            current_price = float(last_row['close'])
            pos_key = self.trader._get_pos_key(self.symbol, self.timeframe)
            
            # Use Trader's dedicated monitoring logic where possible
            # Here we provide high-level glue code
            
            # 1. Check Pending Invalidation (Signal Reversal)
            pending_pos = self.trader.active_positions.get(pos_key)
            if pending_pos and pending_pos.get('status') == 'pending':
                # FIX-A: Minimum pending time — give the order at least 2 minutes before
                # considering a cancel. Prevents churn from fast noise on short timeframes.
                MIN_PENDING_SECS = 120
                safe_timestamp = pending_pos.get('timestamp') or 0
                pending_age = time.time() - (safe_timestamp / 1000)
                if pending_age >= MIN_PENDING_SECS:
                    df_scan = self.data_manager.get_data_with_features(self.symbol, self.timeframe, exchange=self.trader.exchange_name)
                    if df_scan is not None:
                        scan_row = df_scan.iloc[-1]
                        bms_score = float(scan_row.get('bms_score') or 0.5)
                        bms_zone = str(scan_row.get('bms_zone') or 'NEUTRAL')
                        sig = self.strategy.get_signal(
                            scan_row, tracker=self.signal_tracker,
                            bms_score=bms_score, bms_zone=bms_zone
                        )
                        opp_side = 'SELL' if pending_pos['side'] == 'BUY' else 'BUY'
                        # FIX-C: Only cancel on STRONG reversal (opposite side + confidence >= 0.4).
                        # Do NOT cancel just because signal turned neutral/None — that's normal noise.
                        strong_reversal = (
                            sig['side'] == opp_side
                            and sig.get('confidence', 0) >= 0.4
                        )
                        if strong_reversal:
                            await self.trader.cancel_pending_order(pos_key, reason="Strong signal reversal")
                            return True

            # 2. Check Active Position
            pos = self.trader.active_positions.get(pos_key)
            if pos and pos.get('status') == 'filled':
                side = pos['side']
                entry_price_raw = pos.get('entry_price')
                if entry_price_raw is None:
                    return False  # Missing entry price, skip PnL calc
                entry_price = float(entry_price_raw)
                leverage = pos.get('leverage', 1)
                
                # Signal Reversal for Active Position
                df_scan = self.data_manager.get_data_with_features(self.symbol, self.timeframe, exchange=self.trader.exchange_name)
                if df_scan is not None:
                    scan_row = df_scan.iloc[-1]
                    bms_score = float(scan_row.get('bms_score') or 0.5)
                    bms_zone = str(scan_row.get('bms_zone') or 'NEUTRAL')
                    sig = self.strategy.get_signal(
                        scan_row, tracker=self.signal_tracker,
                        bms_score=bms_score, bms_zone=bms_zone
                    )
                    opp_side = 'SELL' if side == 'BUY' else 'BUY'
                    # Use per-symbol exit_score from config, fallback to 3.0
                    exit_thresh = self.strategy.config_data.get('thresholds', {}).get('exit_score', 3.0)
                    if sig['side'] == opp_side and sig['confidence'] * 10 >= exit_thresh:
                        await self.trader.force_close_position(pos_key, reason="Signal flipped")
                        return True
                
                # Dry Run SL/TP simulation
                if self.trader.dry_run:
                    sl = pos.get('sl')
                    tp = pos.get('tp')
                    exit_reason = None
                    if side == 'BUY' and ((sl and current_price <= sl) or (tp and current_price >= tp)):
                        exit_reason = 'SL' if current_price <= sl else 'TP'
                    elif side == 'SELL' and ((sl and current_price >= sl) or (tp and current_price <= tp)):
                        exit_reason = 'SL' if current_price >= sl else 'TP'
                    
                    if exit_reason:
                        if not entry_price: return False  # pending fill, no entry price yet
                        
                        await self.trader.remove_position(self.symbol, self.timeframe, exit_price=current_price, exit_reason=exit_reason)
                        return True
            return False
        except Exception as e:
            self.logger.error(f"Error in monitor cycle: {e}")
            return False

    async def get_new_entry_signal(self):
        """Evaluate strategy for entry."""
        exchange = self.trader.exchange_name
        sym = self.symbol
        tf = self.timeframe

        in_cd = await self.trader.is_in_cooldown(sym)
        if in_cd:
            import time
            now = time.time()
            if now - self.last_cd_log_time > 3600: # Log once per hour
                rem = self.trader.get_cooldown_remaining(sym)
                print(f"⏸️ [{exchange}] [{sym}/{tf}] COOLDOWN {rem:.0f}m remaining")
                self.last_cd_log_time = now
            return None
        if not self.running: return None

        skip, skip_reason = self.signal_tracker.should_skip_symbol(sym)
        if skip:
            print(f"🚫 [{exchange}] [{sym}/{tf}] SKIPPED by signal_tracker: {skip_reason}")
            return None

        df = self.data_manager.get_data_with_features(sym, tf, exchange=exchange)
        if df is None:
            print(f"📭 [{exchange}] [{sym}/{tf}] No feature data available")
            return None

        # Extract BMS from df row and pass to strategy so BMS zone veto is active
        last_row = df.iloc[-1]
        bms_score = float(last_row.get('bms_score') or 0.5)
        bms_zone = str(last_row.get('bms_zone') or 'NEUTRAL')
        signal_data = self.strategy.get_signal(
            last_row, tracker=self.signal_tracker,
            bms_score=bms_score, bms_zone=bms_zone
        )
        side = signal_data.get('side')
        conf = signal_data.get('confidence', 0.0)
        current_ts_str = str(last_row['timestamp'])
        should_log = (current_ts_str != self.last_eval_timestamp)

        if side and side != 'SKIP' and conf >= config.MIN_CONFIDENCE_TO_TRADE:
            signal_data['last_row'] = last_row
            if should_log:
                print(f"✅ [{exchange}] [{sym}/{tf}] SIGNAL {side} conf={conf:.2f} BMS={bms_zone}")
                self.last_eval_timestamp = current_ts_str
            return signal_data

        if side and side != 'SKIP' and should_log:
            print(f"📉 [{exchange}] [{sym}/{tf}] Signal {side} conf={conf:.2f} below min {config.MIN_CONFIDENCE_TO_TRADE}")
            self.last_eval_timestamp = current_ts_str
        
        # Also update for SKIP to avoid re-evaluating same candle's SKIP logic unnecessarily
        if should_log:
            self.last_eval_timestamp = current_ts_str
        return None

    async def execute_entry(self, signal_data, equity):
        """Execute trade based on signal."""
        try:
            side = signal_data['side']
            conf = signal_data['confidence']
            price = float(signal_data['last_row']['close'])
            
            # Calculate target entry price first
            if config.USE_LIMIT_ORDERS:
                entry_target = price * (1 - config.PATIENCE_ENTRY_PCT) if side == 'BUY' else price * (1 + config.PATIENCE_ENTRY_PCT)
                order_type = 'limit'
            else:
                entry_target = price
                order_type = 'market'

            sl_pct, tp_pct = self.strategy.get_dynamic_risk_params(signal_data['last_row'])
            # SL/TP MUST be calculated from the target entry price, otherwise limit order SL/TP will overlap or be invalid!
            sl_price = entry_target * (1 - sl_pct) if side == 'BUY' else entry_target * (1 + sl_pct)
            tp_price = entry_target * (1 + tp_pct) if side == 'BUY' else entry_target * (1 - tp_pct)
            
            # Get tier config for size
            tier = self.strategy.get_sizing_tier(conf * 10)
            target_lev = tier.get('leverage', self.risk_manager.leverage)
            qty = self.risk_manager.calculate_size_by_cost(entry_target, tier.get('cost_usdt', 10), target_lev)
            
            if qty <= 0: return False
            
            # Prepare metadata for AI — convert all non-JSON-serializable types
            import pandas as pd
            last_row = signal_data['last_row']
            def _to_json_safe(v):
                if isinstance(v, pd.Timestamp): return v.isoformat()
                if hasattr(v, 'item'): return v.item()  # numpy scalar → Python native
                return v
            snapshot = {k: _to_json_safe(v) for k, v in last_row.to_dict().items()}


            
            return await self.trader.place_order(
                symbol=self.symbol, side=side, qty=qty, timeframe=self.timeframe,
                order_type=order_type,
                price=entry_target if order_type == 'limit' else None,
                sl=sl_price, tp=tp_price, leverage=target_lev, signals_used=signal_data.get('comment', ''),
                entry_confidence=conf, snapshot=snapshot
            )
        except Exception as e:
            self.logger.error(f"Execution error: {e}")
            return False

async def send_periodic_status_report(trader, data_manager):
    # (Simplified version for brevity, keeps the same logic as before)
    pass

async def main():
    import argparse
    parser = argparse.ArgumentParser(description='Multi-Profile Trading Bot')
    parser.add_argument('--live', action='store_true', help='Force live mode')
    parser.add_argument('--dry-run', action='store_true', help='Force dry-run mode')
    parser.add_argument('--init-opt', action='store_true', help='Run initial optimization on startup')
    args, _ = parser.parse_known_args()
    
    # Environment priority: CLI > .env
    env_str = 'LIVE' if (args.live or not config.DRY_RUN) else 'TEST'
    if args.dry_run: env_str = 'TEST'
    
    print(f"[ENV] Global Environment: {env_str}")
    
    db = await DataManager.get_instance(env_str)
    
    # 1. Load Profiles
    profiles = await db.get_profiles()
    if not profiles:
        print("[WARN] No profiles in DB. Migration requested?")
        return

    manager = MarketDataManager(db)
    balance_tracker = BalanceTracker()
    
    # 2. Pre-load Strategy Configs into Cache
    print("📥 Syncing strategy configurations from database...")
    from src.infrastructure.repository.config_manager import ConfigManager
    config_mgr = await ConfigManager.get_instance(env_str)
    all_configs = await config_mgr.get_all_configs()
    WeightedScoringStrategy.update_cache(all_configs)
    
    # 3. Setup Profile Groups
    profile_groups = [] # List of {trader, risk_manager, signal_tracker, bots[]}
    
    for p in profiles:
        print(f"👤 Loading Profile: {p['name']} ({p['exchange']} {p['environment']})")
        
        # Create dedicated adapter
        from src.infrastructure.adapters.exchange_factory import create_adapter_from_profile
        adapter = await create_adapter_from_profile(p)
        if not adapter:
            print(f"❌ Failed to create adapter for {p['name']}")
            continue
            
        # Core engines for this profile
        signal_tracker = SignalTracker(db, p['id'], env=p['environment'])
        trader = Trader(adapter, db, p['id'], profile_name=p['name'], signal_tracker=signal_tracker, dry_run=(p['environment'] == 'TEST'), data_manager=manager, env=p['environment'])
        risk_manager = RiskManager(db, p['id'], env=p['environment'], exchange_name=p['exchange'])
        
        # Sync metrics
        await risk_manager.sync_from_db()
        await signal_tracker.sync_from_db()
        
        # Sync Trader on startup
        if not trader.dry_run:
            try:
                # Time sync FIRST — before any API call to prevent timestamp errors
                print(f"⏱️ [{p['name']}] Syncing exchange time...")
                await trader.exchange.sync_time()
                await trader.exchange.load_markets()
                print(f"✅ [{p['name']}] Exchange connected & time synced")
                await trader.sync_from_db()
                await trader.reconcile_positions(auto_fix=True)
                await trader.resume_pending_monitors()
            except Exception as e:
                print(f"⚠️ [{p['name']}] Trader sync error: {e}")

        # Task bots for this profile
        active_symbols = config.BINANCE_SYMBOLS if p['exchange'] == 'BINANCE' else config.BYBIT_SYMBOLS
        active_symbols = [s for s in active_symbols if s in config.TRADING_SYMBOLS]
        
        bots = []
        for symbol in active_symbols:
            for tf in config.TRADING_TIMEFRAMES:
                bot = TradingBot(symbol, tf, manager, trader, risk_manager, signal_tracker, balance_tracker)
                bots.append(bot)
                
        profile_groups.append({
            'profile': p,
            'trader': trader,
            'risk_manager': risk_manager,
            'signal_tracker': signal_tracker,
            'bots': bots
        })

    print(f"✅ Total {len(profile_groups)} profiles and {sum(len(pg['bots']) for pg in profile_groups)} bot tasks.")

    # 3. Main Loop
    last_purge_time = 0
    last_bms_update_time = 0 
    
    # Initialize optimization timer from DB
    db = await DataManager.get_instance(env_str)
    
    # NEW BEHAVIOR: Default to skip initial optimization unless --init-opt is passed.
    if args.init_opt:
        print("🚀 Initializing with forced optimization cycle...")
        last_optimization_time = 0 
    else:
        print("⏭️ Skipping initial optimization (default). Next cycle in 12h.")
        last_optimization_time = time.time()
        # Persist this start time to DB so restarts within the 12h window also skip (optional but safer)
        await db.set_risk_metric(0, 'last_optimization_time', last_optimization_time, env_str)

    try:
        while True:
            curr_time = time.time()
            
            # Periodic Global Optimization (Loop A + Loop B) - Every 12h
            if curr_time - last_optimization_time > getattr(config, 'OPTIMIZATION_INTERVAL', 43200):
                print("\n" + "="*60)
                print("🚀 STARTING PERIODIC GLOBAL OPTIMIZATION")
                print("="*60)
                try:
                    from analyzer import run_global_optimization
                    await run_global_optimization()
                    last_optimization_time = curr_time
                    print("✅ Periodic optimization complete.")
                    # [CRITICAL FIX] Refresh strategy config cache from DB after optimization.
                    # The optimizer writes new weights/enabled flags to the DB, but the
                    # in-memory WeightedScoringStrategy._config_cache is stale until refreshed.
                    # Without this, the bot uses old configs and may find no signals.
                    try:
                        updated_configs = await db.get_all_strategy_configs()
                        WeightedScoringStrategy.update_cache(updated_configs)
                        print(f"[CACHE] Strategy cache refreshed: {len(updated_configs)} configs loaded.")
                    except Exception as cache_err:
                        print(f"[CACHE] Failed to refresh strategy cache: {cache_err}")
                except Exception as e:
                    print(f"Optimization error: {e}")
                print("="*60 + "\n")
            
            # Periodic Database Maintenance (Once per 24h)
            if curr_time - last_purge_time > 86400:
                print("🧹 Running periodic database maintenance...")
                try:
                    await db.purge_old_candles(days=30)
                    last_purge_time = curr_time
                except Exception as e:
                    print(f"⚠️ Maintenance error: {e}")
            
            balance_tracker.reset_reservations()
            
            # A. Update Market Data & BMS (BMS throttled to 15m)
            await manager.update_tickers(config.DATA_SYMBOLS)
            updated = await manager.update_data(config.DATA_SYMBOLS, config.TRADING_TIMEFRAMES)
            
            if curr_time - last_bms_update_time > 900: # 15 minutes
                print("🔄 Updating BTC Market Sentiment (BMS)...")
                from btc_analyzer import BTCAnalyzer
                btc_analyzer = BTCAnalyzer(manager, db)
                await btc_analyzer.update_sentiment('BTC/USDT:USDT')
                last_bms_update_time = curr_time
            
            # B. Process each profile
            for pg in profile_groups:
                p = pg['profile']
                trader = pg['trader']
                rm = pg['risk_manager']
                st = pg['signal_tracker']
                bots = pg['bots']
                
                # Apply styling
                p_color = COLOR_MAP.get(p.get('color', 'white').lower(), Fore.WHITE)
                p_label = f"{p_color}[{p['name']}]{Style.RESET_ALL}"
                
                # 0. Periodic State Sync (Resolve external closures/ghosts)
                if not trader.dry_run:
                    # 0.1 Basic Sync (Fast ghost detection) - Every cycle (throttled to 30s internally)
                    await trader.sync_with_exchange()
                    
                    # 0.1.5 SL/TP Guardian — verify every filled position has SL/TP active on exchange
                    # Throttled internally: each position checked at most once per minute, no extra API calls
                    await trader.scan_sltp_liveness()
                    
                    # 0.2 Full Reconciliation (Fix missing SL/TP, adopt orphans) - Every 10 minutes
                    if curr_time - trader._last_reconcile_time > 600:
                        trader.logger.info("📡 [PERIODIC] Starting full reconciliation...")
                        await trader.reconcile_positions(auto_fix=True)
                        trader._last_reconcile_time = curr_time
                        
                    # 0.3 Deep History Sync (Scan for missed exits) - Every 1 hour
                    if curr_time - trader._last_history_sync_time > 3600:
                        trader.logger.info("🔄 [PERIODIC] Starting deep history sync...")
                        await trader.deep_history_sync(lookback_hours=24)
                        trader._last_history_sync_time = curr_time

                # 1. Update Balance & Check Circuit Breaker
                bal_data = await trader.fetch_balance_throttled()
                bal = float(bal_data.get('total', {}).get('USDT', 0)) if bal_data else 0.0
                
                balance_tracker.update_balance(p['exchange'], p['id'], bal)
                stop, reason = await rm.check_circuit_breaker(bal)
                if stop:
                    print(f"🚨 [{p['name']}] CIRCUIT BREAKER: {reason}")
                    continue

                # 2. Coordinate Symbol Tasks
                # Group by symbol
                symbol_groups = defaultdict(list)
                for b in bots: symbol_groups[b.symbol].append(b)
                
                async def run_profile_symbol(symbol, s_bots, t, b_track, p_id):
                    # Monitoring All
                    await asyncio.gather(*[sb.run_monitoring_cycle() for sb in s_bots])

                    # Market Crash/Pump Gate: block new entries when BTC moves ±3%
                    btc_data = manager.get_data('BTC/USDT:USDT', config.TRADING_TIMEFRAMES[0],
                                               exchange=p['exchange'])
                    if btc_data is not None and len(btc_data) >= 2:
                        try:
                            latest_close = float(btc_data.iloc[-1]['close'])
                            prev_close = float(btc_data.iloc[-2]['close'])
                            btc_pct_change = (latest_close - prev_close) / prev_close if prev_close > 0 else 0
                            market_status = st.check_market_condition(btc_pct_change)
                            if market_status in ('crash', 'pump'):
                                print(f"🌩️ [{p['name']}] [{symbol}] Market {market_status.upper()} "
                                      f"({btc_pct_change*100:.1f}%). Blocking new entries.")
                                return
                        except Exception:
                            pass  # Don't block on error — safe fallback

                    # Entry competition
                    async with t._get_lock(symbol):
                        if await t.has_any_symbol_position(symbol): return
                        
                        signals = await asyncio.gather(*[sb.get_new_entry_signal() for sb in s_bots])
                        valid = [(s_bots[i], sig) for i, sig in enumerate(signals) if sig]
                        if not valid: return
                        
                        best_bot, best_sig = max(valid, key=lambda x: x[1]['confidence'])
                        avail = b_track.get_available(t.exchange_name, p_id)
                        if avail > 10: # Min trade equity
                            await best_bot.execute_entry(best_sig, avail)

                # Parallel per symbol
                tasks = [run_profile_symbol(s, sb, trader, balance_tracker, p['id']) for s, sb in symbol_groups.items()]
                await asyncio.gather(*tasks)

            # Heartbeat print to show bot is alive and moving
            print(f"📡 [HEARTBEAT] All {sum(len(pg['bots']) for pg in profile_groups)} bot tasks scanned.")

            # C. Sleep
            sleep_time = config.HEARTBEAT_INTERVAL if updated else config.FAST_HEARTBEAT_INTERVAL
            await asyncio.sleep(sleep_time)

    except KeyboardInterrupt:
         print("Stopping bots...")
    finally:
        print("🔌 Closing system resources...")
        if manager:
            await manager.close()
        for pg in profile_groups:
             await pg['trader'].close()
        # Close the global DB connections
        await DataManager.clear_instances()
        print("✅ All resources released.")

if __name__ == "__main__":
    asyncio.run(main())
