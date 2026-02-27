"""
Signal Performance Tracker - Adaptive Learning System v3.0
Tracks which signals lead to wins/losses and adjusts weights dynamically.
Integrated with DataManager for persistent SQLite storage.
"""

import json
import inspect
import os
import sys
import time
import asyncio
import logging
from datetime import datetime, timedelta
from collections import defaultdict

# Learning parameters
LOOKBACK_TRADES = 20  # Consider last N trades per signal
MIN_TRADES_FOR_PENALTY = 3  # Need at least N trades before penalizing
PENALTY_THRESHOLD = 0.35  # If win rate < 35%, penalize signal
BOOST_THRESHOLD = 0.70  # If win rate > 70%, boost signal
WEIGHT_PENALTY = 0.5  # Multiply weight by this when penalizing
WEIGHT_BOOST = 1.2  # Multiply weight by this when boosting

# Adaptive trigger parameters
LOSS_TRIGGER_COUNT = 2  # Trigger analysis after N consecutive losses
MARKET_CRASH_THRESHOLD = 0.03  # ±3% BTC change = crash/pump

class SignalTracker:
    def __init__(self, db, profile_id: int, env: str = 'LIVE'):
        self.db = db
        self.profile_id = profile_id
        self.env = env.upper()
        self.logger = logging.getLogger("SignalTracker")
        
        # In-memory cache for fast calculations
        self.trades = [] # Last 1000 trades
        self.signal_stats = {} # Aggregated stats per signal
        
        self.consecutive_losses = 0
        self.recent_loss_symbols = []
        self._analysis_callback = None
        self._position_adjust_callback = None

    async def sync_from_db(self):
        """
        Load recent trades from DB to populate in-memory stats.
        Note: DataManager needs a method to fetch trade history.
        """
        try:
            # We'll assume DataManager has get_trade_history or we use direct query for now
            # Actually, let's implement get_trade_history in DataManager if missing.
            # For now, we'll use a direct select if DataManager allows or just let it start fresh.
            # Re-calculating stats from past 1000 trades is best.
            
            # TODO: Implement db.get_trade_history(profile_id, limit=1000)
            # For this phase, we'll start with empty and let it build up, 
            # or migration tool would have populated it.
            pass
        except Exception as e:
            self.logger.error(f"Error syncing signal stats from DB: {e}")

    def set_analysis_callback(self, callback):
        self._analysis_callback = callback
    
    def set_position_adjust_callback(self, callback):
        self._position_adjust_callback = callback

    async def record_trade(self, symbol, timeframe, side, signals_used, result, pnl_pct,
                 btc_change=None, snapshot=None,
                 pnl_usdt=None, entry_price=None, exit_price=None,
                 qty=None, exit_reason=None, entry_time=None, exit_time=None,
                 sl_original=None, sl_final=None, sl_move_count=0,
                 sl_tightened=False, max_pnl_pct=0, pos_key=None, leverage=None):
        """
        Record a completed trade for learning and persistence.
        """
        trade_data = {
            'profile_id': self.profile_id,
            'symbol': symbol,
            'timeframe': timeframe,
            'side': side,
            'signals': signals_used, # Will be JSON stringified in meta or handled by insert_trade_history
            'result': result,
            'pnl_pct': pnl_pct,
            'pnl_usdt': pnl_usdt,
            'entry_price': entry_price,
            'exit_price': exit_price,
            'qty': qty,
            'exit_reason': exit_reason,
            'entry_time': entry_time,
            'exit_time': exit_time,
            'status': 'CLOSED',
            'pos_key': pos_key,
            'leverage': leverage,
            'meta': {
                'sl_original': sl_original,
                'sl_final': sl_final,
                'sl_move_count': sl_move_count,
                'sl_tightened': sl_tightened,
                'max_pnl_pct': max_pnl_pct,
                'btc_change': btc_change,
                'signals': signals_used # Duplicate for meta ease
            }
        }
        
        # Persistence
        try:
            trade_id = await self.db.insert_trade_history(trade_data)
            if snapshot and trade_id:
                await self.db.log_ai_snapshot(trade_id, json.dumps(snapshot), 0.0) # confidence meta should be in snapshot
        except Exception as e:
            self.logger.error(f"Failed to persist trade to DB: {e}")

        # In-memory update for signals
        self._update_signal_stats(signals_used, result)
        
        # Local cache for symbol performance
        self.trades.append({
            'timestamp': datetime.now().isoformat(),
            'symbol': symbol,
            'result': result,
            'side': side
        })
        if len(self.trades) > 1000:
            self.trades = self.trades[-1000:]
            
        # === ADAPTIVE LEARNING ===
        if result == 'WIN':
            self.consecutive_losses = 0
            self.recent_loss_symbols = []
        else:
            self.consecutive_losses += 1
            if symbol not in self.recent_loss_symbols:
                self.recent_loss_symbols.append(symbol)
            
            if self.consecutive_losses >= LOSS_TRIGGER_COUNT:
                self.logger.warning(f"⚠️ [ADAPTIVE] {self.consecutive_losses} consecutive losses detected")
                await self._trigger_adaptive_check(btc_change)

    async def _trigger_adaptive_check(self, btc_change=None):
        """Trigger adaptive analysis after consecutive losses."""
        market_status = self.check_market_condition(btc_change)
        
        if market_status != 'normal':
            self.logger.info(f"   ↳ BTC {market_status} ({btc_change*100:.1f}%) - Skipping analysis")
            self._reset_loss_counter()
            return
        
        symbols_to_analyze = list(self.recent_loss_symbols)
        self.logger.info(f"   ↳ Analyzing: {', '.join(symbols_to_analyze)}")
        
        callback_success = True
        if self._analysis_callback:
            try:
                if inspect.iscoroutinefunction(self._analysis_callback):
                    await self._analysis_callback(symbols_to_analyze)
                else:
                    self._analysis_callback(symbols_to_analyze)
            except Exception as e:
                self.logger.error(f"   ✗ Analyzer error: {e}")
                callback_success = False
        
        if self._position_adjust_callback:
            try:
                if inspect.iscoroutinefunction(self._position_adjust_callback):
                    await self._position_adjust_callback()
                else:
                    self._position_adjust_callback()
            except Exception as e:
                self.logger.error(f"   ✗ Position adjust error: {e}")
                callback_success = False
        
        if callback_success:
            self._reset_loss_counter()

    def _reset_loss_counter(self):
        self.consecutive_losses = 0
        self.recent_loss_symbols = []

    def check_market_condition(self, btc_change):
        if btc_change is None: return 'normal'
        if btc_change <= -MARKET_CRASH_THRESHOLD: return 'crash'
        if btc_change >= MARKET_CRASH_THRESHOLD: return 'pump'
        return 'normal'

    def _update_signal_stats(self, signals, result):
        if not signals: return
        for signal in signals:
            if signal not in self.signal_stats:
                self.signal_stats[signal] = {
                    'total': 0, 'wins': 0, 'losses': 0, 'recent_results': []
                }
            stats = self.signal_stats[signal]
            stats['total'] += 1
            if result == 'WIN': stats['wins'] += 1
            else: stats['losses'] += 1
            
            stats['recent_results'].append(1 if result == 'WIN' else 0)
            if len(stats['recent_results']) > LOOKBACK_TRADES:
                stats['recent_results'] = stats['recent_results'][-LOOKBACK_TRADES:]

    def get_signal_performance(self, signal_name):
        if signal_name not in self.signal_stats: return None
        stats = self.signal_stats[signal_name]
        recent = stats['recent_results']
        return {
            'total_trades': stats['total'],
            'all_time_wr': stats['wins'] / stats['total'] if stats['total'] > 0 else 0,
            'recent_trades': len(recent),
            'recent_wr': sum(recent) / len(recent) if recent else 0.5
        }

    def get_weight_multiplier(self, signal_name):
        perf = self.get_signal_performance(signal_name)
        if not perf or perf['recent_trades'] < MIN_TRADES_FOR_PENALTY: return 1.0
        wr = perf['recent_wr']
        if wr < PENALTY_THRESHOLD: return WEIGHT_PENALTY
        elif wr > BOOST_THRESHOLD: return WEIGHT_BOOST
        return 1.0

    def adjust_weights(self, weights_dict):
        adjusted = {}
        for signal, weight in weights_dict.items():
            multiplier = self.get_weight_multiplier(signal)
            adjusted[signal] = weight * multiplier
        return adjusted

    def get_symbol_recent_performance(self, symbol, lookback_hours=24):
        cutoff = datetime.now() - timedelta(hours=lookback_hours)
        recent_trades = [
            t for t in self.trades
            if t['symbol'] == symbol and datetime.fromisoformat(t['timestamp']) > cutoff
        ]
        if not recent_trades: return None
        wins = sum(1 for t in recent_trades if t['result'] == 'WIN')
        return {
            'trades': len(recent_trades),
            'win_rate': wins / len(recent_trades),
            'last_result': recent_trades[-1]['result'] if recent_trades else None
        }

    def get_last_trade_side(self, symbol):
        recent = [t for t in self.trades if t['symbol'] == symbol]
        return recent[-1]['side'] if recent else None

    def should_skip_symbol(self, symbol, min_wr=0.3, min_trades=3):
        perf = self.get_symbol_recent_performance(symbol, lookback_hours=12)
        if not perf: return False, "No recent data"
        if perf['trades'] < min_trades: return False, f"Only {perf['trades']} trades"
        if perf['win_rate'] < min_wr:
            return True, f"Poor WR: {perf['win_rate']*100:.0f}%"
        return False, "WR OK"

    def print_summary(self):
        print(f"\n{'='*40}\nSIGNAL PERFORMANCE SUMMARY\n{'='*40}")
        if not self.signal_stats:
            print("No data yet.")
            return
        sorted_signals = sorted(
            self.signal_stats.items(),
            key=lambda x: sum(x[1]['recent_results']) / len(x[1]['recent_results']) if x[1]['recent_results'] else 0,
            reverse=True
        )
        print(f"{'Signal':<20} {'Total':>6} {'Recent WR':>10}")
        for signal, data in sorted_signals[:15]:
            recent = data['recent_results']
            wr = sum(recent) / len(recent) * 100 if recent else 0
            print(f"{signal[:20]:<20} {data['total']:>6} {wr:>9.1f}%")
