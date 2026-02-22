"""
Signal Performance Tracker - Adaptive Learning System v2.0
Tracks which signals lead to wins/losses and adjusts weights dynamically.

NEW in v2.0:
- Loss counter: Trigger analysis after 2 consecutive losses (any symbol)
- Market condition check: Skip if BTC crash/pump ±3%
- Mini-analyzer integration: Re-optimize symbols after losses
- Position adjustment: Tighten SL or force close on signal reversal
"""

import json
import os
import time
from datetime import datetime, timedelta
from collections import defaultdict

TRACKER_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'signal_performance.json')

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

# Global rate limit tracker
LAST_ADAPTIVE_LOG_TIME = 0


class SignalTracker:
    def __init__(self):
        self.data = self._load()
        self.consecutive_losses = 0  # Reset on win
        self.recent_loss_symbols = []  # Track which symbols lost recently
        self._analysis_callback = None  # Callback for triggering analysis
        self._position_adjust_callback = None  # Callback for adjusting positions
    
    def set_analysis_callback(self, callback):
        """Set callback function to trigger when losses reach threshold."""
        self._analysis_callback = callback
    
    def set_position_adjust_callback(self, callback):
        """Set callback for position adjustments."""
        self._position_adjust_callback = callback
    
    def _load(self):
        """Load signal performance data from file."""
        if os.path.exists(TRACKER_FILE):
            try:
                with open(TRACKER_FILE, 'r') as f:
                    return json.load(f)
            except Exception as e:
                print(f"Error loading signal tracker: {e}")
        return {
            'trades': [],  # List of trade records
            'signal_stats': {}  # Aggregated stats per signal
        }
    
    def _save(self):
        """Save signal performance data to file."""
        try:
            with open(TRACKER_FILE, 'w') as f:
                json.dump(self.data, f, indent=2)
        except Exception as e:
            print(f"Error saving signal tracker: {e}")
    
    def record_trade(self, symbol, timeframe, side, signals_used, result, pnl_pct,
                 btc_change=None, snapshot=None,
                 pnl_usdt=None, entry_price=None, exit_price=None,
                 qty=None, exit_reason=None, entry_time=None, exit_time=None,
                 sl_original=None, sl_final=None, sl_move_count=0,
                 sl_tightened=False, max_pnl_pct=0):
        """
        Record a completed trade for learning.

        Args:
            symbol: Trading pair (e.g., 'NEAR/USDT')
            timeframe: Timeframe (e.g., '1h')
            side: 'BUY' or 'SELL'
            signals_used: List of signal names that triggered entry
            result: 'WIN' (TP hit) or 'LOSS' (SL hit)
            pnl_pct: PnL percentage
            btc_change: BTC 1h price change (optional, for market condition check)
            snapshot: Dictionary of normalized features at entry (for Neural Net training)
            pnl_usdt: Realised PnL in USDT (accounting, replaces trade_history.json)
            entry_price: Entry fill price
            exit_price: Exit fill price
            qty: Position size (base asset)
            exit_reason: Why the trade closed (TP, SL, Signal Flip, etc.)
            entry_time: Entry timestamp (epoch ms)
            exit_time: Exit timestamp (epoch ms)
        """
        trade = {
            'timestamp': datetime.now().isoformat(),
            'symbol': symbol,
            'timeframe': timeframe,
            'side': side,
            'signals': signals_used,
            'result': result,
            'pnl_pct': pnl_pct,
            # Accounting fields (unified store – replaces trade_history.json)
            'pnl_usdt': pnl_usdt,
            'entry_price': entry_price,
            'exit_price': exit_price,
            'qty': qty,
            'exit_reason': exit_reason,
            'entry_time': entry_time,
            'exit_time': exit_time,
            # Dynamic Context (v4.0)
            'sl_original': sl_original,
            'sl_final': sl_final,
            'sl_move_count': sl_move_count,
            'sl_tightened': sl_tightened,
            'max_pnl_pct': max_pnl_pct,
        }

        # RL UPGRADE: Save feature snapshot for training
        if snapshot:
            trade['snapshot'] = snapshot
        
        self.data['trades'].append(trade)
        
        # Keep only last 1000 trades (increased for RL data collection)
        if len(self.data['trades']) > 1000:
            self.data['trades'] = self.data['trades'][-1000:]
        
        # Update signal stats
        self._update_signal_stats(signals_used, result)
        
        self._save()
        
        # === ADAPTIVE LEARNING v2.0 ===
        if result == 'WIN':
            # Reset counter on win (silent)
            self.consecutive_losses = 0
            self.recent_loss_symbols = []
        else:
            # Increment counter on loss
            self.consecutive_losses += 1
            if symbol not in self.recent_loss_symbols:
                self.recent_loss_symbols.append(symbol)
            
            # Only log when approaching threshold
            if self.consecutive_losses >= LOSS_TRIGGER_COUNT:
                print(f"\n⚠️  [ADAPTIVE] {self.consecutive_losses} consecutive losses detected")
                self._trigger_adaptive_check(btc_change)
    
    def _trigger_adaptive_check(self, btc_change=None):
        """Trigger adaptive analysis after consecutive losses."""
        # Step 1: Check market condition
        market_status = self.check_market_condition(btc_change)
        
        if market_status == 'crash':
            print(f"   ↳ BTC crash ({btc_change*100:.1f}%) - Skipping analysis (market fault)")
            self._reset_loss_counter()
            return
        elif market_status == 'pump':
            print(f"   ↳ BTC pump ({btc_change*100:.1f}%) - Skipping analysis (market fault)")
            self._reset_loss_counter()
            return
        
        # Step 2: Trigger callbacks for analysis and position adjustment
        symbols_to_analyze = list(self.recent_loss_symbols)
        print(f"   ↳ Analyzing: {', '.join(symbols_to_analyze)}")
        
        callback_success = True
        if self._analysis_callback:
            try:
                self._analysis_callback(symbols_to_analyze)
                print(f"   ✓ Mini-analyzer completed")
            except Exception as e:
                print(f"   ✗ Analyzer error: {e}")
                callback_success = False
        
        if self._position_adjust_callback:
            try:
                self._position_adjust_callback()
                print(f"   ✓ Position check completed")
            except Exception as e:
                print(f"   ✗ Position adjust error: {e}")
                callback_success = False
        
        # Reset counter
        if callback_success:
            self._reset_loss_counter()
            print(f"   ✓ Adaptive cycle complete\n")
        else:
            print(f"   ⚠️  Keeping loss counter due to errors\n")
    
    def _reset_loss_counter(self):
        """Reset loss counter and recent symbols."""
        self.consecutive_losses = 0
        self.recent_loss_symbols = []
    
    def check_market_condition(self, btc_change):
        """
        Check if market is in crash/pump condition.
        
        Args:
            btc_change: BTC 1h price change as decimal (e.g., -0.03 = -3%)
        
        Returns:
            'crash' | 'pump' | 'normal'
        """
        if btc_change is None:
            return 'normal'  # No data, assume normal
        
        if btc_change <= -MARKET_CRASH_THRESHOLD:
            return 'crash'
        elif btc_change >= MARKET_CRASH_THRESHOLD:
            return 'pump'
        else:
            return 'normal'
    
    def _update_signal_stats(self, signals, result):
        """Update per-signal statistics."""
        for signal in signals:
            if signal not in self.data['signal_stats']:
                self.data['signal_stats'][signal] = {
                    'total': 0,
                    'wins': 0,
                    'losses': 0,
                    'recent_results': []  # Last N results
                }
            
            stats = self.data['signal_stats'][signal]
            stats['total'] += 1
            
            if result == 'WIN':
                stats['wins'] += 1
            else:
                stats['losses'] += 1
            
            # Keep recent results (1 = win, 0 = loss)
            stats['recent_results'].append(1 if result == 'WIN' else 0)
            if len(stats['recent_results']) > LOOKBACK_TRADES:
                stats['recent_results'] = stats['recent_results'][-LOOKBACK_TRADES:]
    
    def get_signal_performance(self, signal_name):
        """Get performance stats for a specific signal."""
        if signal_name not in self.data['signal_stats']:
            return None
        
        stats = self.data['signal_stats'][signal_name]
        recent = stats['recent_results']
        
        return {
            'total_trades': stats['total'],
            'all_time_wr': stats['wins'] / stats['total'] if stats['total'] > 0 else 0,
            'recent_trades': len(recent),
            'recent_wr': sum(recent) / len(recent) if recent else 0.5
        }
    
    def get_weight_multiplier(self, signal_name):
        """
        Get weight multiplier for a signal based on recent performance.
        Returns 1.0 for neutral, <1.0 for penalty, >1.0 for boost.
        """
        perf = self.get_signal_performance(signal_name)
        
        if perf is None:
            return 1.0  # No data, neutral
        
        if perf['recent_trades'] < MIN_TRADES_FOR_PENALTY:
            return 1.0  # Not enough data
        
        wr = perf['recent_wr']
        
        if wr < PENALTY_THRESHOLD:
            return WEIGHT_PENALTY
        elif wr > BOOST_THRESHOLD:
            return WEIGHT_BOOST
        
        return 1.0
    
    def adjust_weights(self, weights_dict):
        """
        Apply adaptive adjustments to a weights dictionary.
        Returns adjusted weights.
        """
        adjusted = {}
        changes = []
        
        for signal, weight in weights_dict.items():
            multiplier = self.get_weight_multiplier(signal)
            adjusted[signal] = weight * multiplier
            
            if multiplier != 1.0:
                changes.append(f"{signal}: {weight:.2f} -> {adjusted[signal]:.2f}")
        
        # Silent - only log if many changes, and rate-limit to once per hour to prevent spam
        if len(changes) > 5:
            global LAST_ADAPTIVE_LOG_TIME
            current_time = time.time()
            if (current_time - LAST_ADAPTIVE_LOG_TIME) > 3600:
                print(f"[ADAPTIVE] Adjusted {len(changes)} signal weights")
                LAST_ADAPTIVE_LOG_TIME = current_time
        
        return adjusted
    
    def get_symbol_recent_performance(self, symbol, lookback_hours=24):
        """Get recent win rate for a specific symbol."""
        cutoff = datetime.now() - timedelta(hours=lookback_hours)
        
        recent_trades = [
            t for t in self.data['trades']
            if t['symbol'] == symbol and datetime.fromisoformat(t['timestamp']) > cutoff
        ]
        
        if not recent_trades:
            return None
        
        wins = sum(1 for t in recent_trades if t['result'] == 'WIN')
        return {
            'trades': len(recent_trades),
            'win_rate': wins / len(recent_trades),
            'last_result': recent_trades[-1]['result'] if recent_trades else None
        }
    
    def get_last_trade_side(self, symbol):
        """Get the side of the absolute latest trade for a symbol."""
        recent_trades = [t for t in self.data['trades'] if t['symbol'] == symbol]
        if not recent_trades:
            return None
        return recent_trades[-1]['side']

    def should_skip_symbol(self, symbol, min_wr=0.3, min_trades=3):
        """
        Check if we should skip trading this symbol due to poor recent performance.
        """
        perf = self.get_symbol_recent_performance(symbol, lookback_hours=12)
        
        if perf is None:
            return False, "No recent data"
        
        if perf['trades'] < min_trades:
            return False, f"Only {perf['trades']} trades"
        
        if perf['win_rate'] < min_wr:
            return True, f"Poor WR: {perf['win_rate']*100:.0f}% in last {perf['trades']} trades"
        
        return False, f"WR OK: {perf['win_rate']*100:.0f}%"
    
    def print_summary(self):
        """Print performance summary."""
        print("\n" + "="*60)
        print("SIGNAL PERFORMANCE SUMMARY")
        print("="*60)
        
        stats = self.data.get('signal_stats', {})
        if not stats:
            print("No data yet.")
            return
        
        # Sort by recent win rate
        sorted_signals = sorted(
            stats.items(),
            key=lambda x: sum(x[1]['recent_results']) / len(x[1]['recent_results']) if x[1]['recent_results'] else 0,
            reverse=True
        )
        
        print(f"{'Signal':<30} {'Total':>6} {'All WR':>8} {'Recent':>8}")
        print("-"*60)
        
        for signal, data in sorted_signals[:20]:  # Top 20
            total = data['total']
            all_wr = data['wins'] / total * 100 if total > 0 else 0
            recent = data['recent_results']
            recent_wr = sum(recent) / len(recent) * 100 if recent else 0
            
            icon = "[+]" if recent_wr >= 60 else "[-]" if recent_wr < 40 else "[=]"
            print(f"{signal:<30} {total:>6} {all_wr:>7.0f}% {icon}{recent_wr:>6.0f}%")
        
        print("="*60 + "\n")


# Global tracker instance
tracker = SignalTracker()
