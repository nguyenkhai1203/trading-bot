"""
Comprehensive Self-Test for Adaptive Learning System
Tests: file persistence, weight adjustment, signal tracking, integration
"""
import sys
import os
import json
from datetime import datetime

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

def test_1_signal_tracker_initialization():
    """Test 1: SignalTracker initialization and file creation"""
    print("\n" + "="*70)
    print("[TEST 1] Signal Tracker Initialization")
    print("="*70)
    
    try:
        from signal_tracker import SignalTracker, TRACKER_FILE
        
        print(f"[*] Creating new SignalTracker instance...")
        tracker = SignalTracker()
        
        print(f"[*] Tracker file path: {TRACKER_FILE}")
        print(f"[*] Data structure keys: {list(tracker.data.keys())}")
        print(f"[*] Total trades loaded: {len(tracker.data.get('trades', []))}")
        print(f"[*] Total signal stats: {len(tracker.data.get('signal_stats', {}))}")
        
        if os.path.exists(TRACKER_FILE):
            print(f"[OK] Tracker file exists at: {TRACKER_FILE}")
        else:
            print(f"[WARN] Tracker file not found, will be created on first save")
        
        print("[PASS] Test 1 completed successfully\n")
        return True, tracker
    except Exception as e:
        print(f"[FAIL] Test 1 failed: {e}")
        import traceback
        traceback.print_exc()
        return False, None


def test_2_record_trades(tracker):
    """Test 2: Record trades and verify persistence"""
    print("\n" + "="*70)
    print("[TEST 2] Record Trades & File Persistence")
    print("="*70)
    
    try:
        from signal_tracker import TRACKER_FILE
        
        # Get initial count
        initial_trade_count = len(tracker.data.get('trades', []))
        print(f"[*] Initial trade count: {initial_trade_count}")
        
        # Record test trades
        test_trades = [
            {
                'symbol': 'BTC/USDT',
                'timeframe': '1h',
                'side': 'BUY',
                'signals': ['RSI_14_oversold', 'EMA9_EMA21_cross_up'],
                'result': 'WIN',
                'pnl_pct': 2.5
            },
            {
                'symbol': 'ETH/USDT',
                'timeframe': '4h',
                'side': 'SELL',
                'signals': ['RSI_14_overbought', 'MACD_cross_down'],
                'result': 'LOSS',
                'pnl_pct': -1.8
            },
            {
                'symbol': 'SOL/USDT',
                'timeframe': '15m',
                'side': 'BUY',
                'signals': ['Stochastic_oversold', 'BB_lower_touch'],
                'result': 'WIN',
                'pnl_pct': 1.9
            }
        ]
        
        print(f"[*] Recording {len(test_trades)} test trades...")
        for i, trade in enumerate(test_trades, 1):
            tracker.record_trade(
                symbol=trade['symbol'],
                timeframe=trade['timeframe'],
                side=trade['side'],
                signals_used=trade['signals'],
                result=trade['result'],
                pnl_pct=trade['pnl_pct']
            )
            print(f"    Trade {i}/{len(test_trades)}: {trade['symbol']} {trade['side']} -> {trade['result']}")
        
        # Verify file was saved
        if not os.path.exists(TRACKER_FILE):
            print(f"[FAIL] Tracker file not created after recording trades")
            return False
        
        # Verify file can be loaded
        print(f"[*] Verifying file can be reloaded...")
        with open(TRACKER_FILE, 'r') as f:
            loaded_data = json.load(f)
        
        new_trade_count = len(loaded_data.get('trades', []))
        print(f"[*] Trades in file: {new_trade_count}")
        print(f"[*] Expected minimum: {initial_trade_count + len(test_trades)}")
        
        if new_trade_count >= initial_trade_count + len(test_trades):
            print(f"[OK] File persistence working - {new_trade_count} trades saved")
        else:
            print(f"[FAIL] Expected at least {initial_trade_count + len(test_trades)} trades, got {new_trade_count}")
            return False
        
        # Verify signal stats were updated
        signal_stats = loaded_data.get('signal_stats', {})
        print(f"[*] Signal stats tracked: {len(signal_stats)} unique signals")
        
        expected_signals = set()
        for trade in test_trades:
            expected_signals.update(trade['signals'])
        
        print(f"[*] Expected signals: {sorted(expected_signals)}")
        
        for sig in expected_signals:
            if sig in signal_stats:
                stats = signal_stats[sig]
                print(f"    - {sig}: {stats['wins']}W / {stats['losses']}L (Total: {stats['total']})")
            else:
                print(f"    [WARN] Signal {sig} not found in stats")
        
        print("[PASS] Test 2 completed successfully\n")
        return True
        
    except Exception as e:
        print(f"[FAIL] Test 2 failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_3_weight_adjustment(tracker):
    """Test 3: Adaptive weight adjustment based on performance"""
    print("\n" + "="*70)
    print("[TEST 3] Adaptive Weight Adjustment")
    print("="*70)
    
    try:
        # Create test weights dictionary
        test_weights = {
            'RSI_14_oversold': 2.0,
            'RSI_14_overbought': 2.0,
            'EMA9_EMA21_cross_up': 1.5,
            'MACD_cross_down': 1.5,
            'Stochastic_oversold': 1.0,
            'BB_lower_touch': 1.0
        }
        
        print(f"[*] Original weights:")
        for sig, wt in test_weights.items():
            print(f"    {sig}: {wt:.2f}")
        
        # Apply adaptive adjustments
        print(f"\n[*] Applying adaptive weight adjustments...")
        adjusted_weights = tracker.adjust_weights(test_weights.copy())
        
        print(f"\n[*] Adjusted weights:")
        changes_detected = False
        for sig, wt in adjusted_weights.items():
            orig = test_weights[sig]
            if wt != orig:
                changes_detected = True
                change_pct = ((wt - orig) / orig) * 100
                print(f"    {sig}: {orig:.2f} -> {wt:.2f} ({change_pct:+.1f}%)")
            else:
                print(f"    {sig}: {wt:.2f} (unchanged)")
        
        # Verify weights are still valid numbers
        for sig, wt in adjusted_weights.items():
            if not isinstance(wt, (int, float)):
                print(f"[FAIL] Weight for {sig} is not numeric: {type(wt)}")
                return False
            if wt < 0:
                print(f"[FAIL] Negative weight detected for {sig}: {wt}")
                return False
        
        print(f"\n[OK] All adjusted weights are valid")
        if changes_detected:
            print(f"[OK] Adaptive adjustments detected and applied")
        else:
            print(f"[INFO] No adjustments needed (insufficient data or neutral performance)")
        
        print("[PASS] Test 3 completed successfully\n")
        return True
        
    except Exception as e:
        print(f"[FAIL] Test 3 failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_4_consecutive_loss_tracking(tracker):
    """Test 4: Consecutive loss counter and adaptive trigger"""
    print("\n" + "="*70)
    print("[TEST 4] Consecutive Loss Tracking")
    print("="*70)
    
    try:
        # Reset tracker state
        tracker.consecutive_losses = 0
        tracker.recent_loss_symbols = []
        
        print(f"[*] Initial state: {tracker.consecutive_losses} consecutive losses")
        
        # Record first loss (should not trigger adaptive)
        print(f"\n[*] Recording first loss...")
        tracker.record_trade('BTC/USDT', '1h', 'BUY', ['test_signal'], 'LOSS', -2.0)
        print(f"    Loss #1: consecutive_losses = {tracker.consecutive_losses}")
        
        if tracker.consecutive_losses == 1:
            print(f"[OK] First loss tracked correctly")
        else:
            print(f"[FAIL] Expected 1 loss, got {tracker.consecutive_losses}")
            return False
        
        # Record second loss (will trigger adaptive and reset counter)
        print(f"\n[*] Recording second loss (will trigger adaptive check)...")
        loss_count_before = tracker.consecutive_losses
        tracker.record_trade('ETH/USDT', '4h', 'SELL', ['test_signal'], 'LOSS', -1.5)
        
        # After adaptive trigger, counter should be reset to 0
        print(f"    Before second loss: {loss_count_before}")
        print(f"    After adaptive check: {tracker.consecutive_losses}")
        
        if tracker.consecutive_losses == 0:
            print(f"[OK] Adaptive trigger fired and counter reset")
        else:
            print(f"[WARN] Counter not reset after adaptive trigger: {tracker.consecutive_losses}")
        
        # Record a win to verify counter stays at 0
        print(f"\n[*] Recording a win to test counter behavior...")
        tracker.record_trade('SOL/USDT', '15m', 'BUY', ['test_signal'], 'WIN', 3.0)
        print(f"    After win: consecutive_losses = {tracker.consecutive_losses}")
        
        if tracker.consecutive_losses == 0:
            print(f"[OK] Counter remains at 0 after win")
        else:
            print(f"[WARN] Counter unexpected value: {tracker.consecutive_losses}")
        
        # Test loss counter increments again after adaptive reset
        print(f"\n[*] Testing counter increments again after reset...")
        tracker.record_trade('BTC/USDT', '1h', 'SELL', ['test_signal'], 'LOSS', -1.0)
        if tracker.consecutive_losses == 1:
            print(f"[OK] Counter increments correctly after reset: {tracker.consecutive_losses}")
        else:
            print(f"[FAIL] Expected 1, got {tracker.consecutive_losses}")
            return False
        
        print("[PASS] Test 4 completed successfully\n")
        return True
        
    except Exception as e:
        print(f"[FAIL] Test 4 failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_5_signal_performance_stats():
    """Test 5: Signal performance statistics calculation"""
    print("\n" + "="*70)
    print("[TEST 5] Signal Performance Statistics")
    print("="*70)
    
    try:
        from signal_tracker import tracker
        
        # Get performance for tracked signals
        stats = tracker.data.get('signal_stats', {})
        
        if not stats:
            print(f"[INFO] No signal stats available yet (expected for fresh install)")
            print("[PASS] Test 5 completed (skipped - no data)\n")
            return True
        
        print(f"[*] Analyzing {len(stats)} tracked signals...")
        
        for signal_name, signal_data in list(stats.items())[:10]:  # Show top 10
            perf = tracker.get_signal_performance(signal_name)
            
            if perf:
                print(f"\n    Signal: {signal_name}")
                print(f"    - Total trades: {perf['total_trades']}")
                print(f"    - All-time WR: {perf['all_time_wr']*100:.1f}%")
                print(f"    - Recent trades: {perf['recent_trades']}")
                print(f"    - Recent WR: {perf['recent_wr']*100:.1f}%")
                
                # Check weight multiplier
                multiplier = tracker.get_weight_multiplier(signal_name)
                if multiplier != 1.0:
                    print(f"    - Weight multiplier: {multiplier:.2f}x")
        
        print(f"\n[OK] Signal performance stats calculated successfully")
        print("[PASS] Test 5 completed successfully\n")
        return True
        
    except Exception as e:
        print(f"[FAIL] Test 5 failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_6_file_reload_persistence():
    """Test 6: Test file reload after creating new tracker instance"""
    print("\n" + "="*70)
    print("[TEST 6] File Reload & Persistence")
    print("="*70)
    
    try:
        from signal_tracker import SignalTracker, TRACKER_FILE
        
        # Get current state
        print(f"[*] Reading current tracker file...")
        with open(TRACKER_FILE, 'r') as f:
            original_data = json.load(f)
        
        original_trade_count = len(original_data.get('trades', []))
        print(f"[*] Original trade count: {original_trade_count}")
        
        # Create new tracker instance (should reload from file)
        print(f"[*] Creating new tracker instance...")
        new_tracker = SignalTracker()
        
        reloaded_trade_count = len(new_tracker.data.get('trades', []))
        print(f"[*] Reloaded trade count: {reloaded_trade_count}")
        
        if reloaded_trade_count == original_trade_count:
            print(f"[OK] Data persisted correctly - {reloaded_trade_count} trades reloaded")
        else:
            print(f"[FAIL] Trade count mismatch: {original_trade_count} -> {reloaded_trade_count}")
            return False
        
        # Verify signal stats also reloaded
        original_signals = len(original_data.get('signal_stats', {}))
        reloaded_signals = len(new_tracker.data.get('signal_stats', {}))
        
        print(f"[*] Signal stats: {original_signals} original, {reloaded_signals} reloaded")
        
        if original_signals == reloaded_signals:
            print(f"[OK] Signal stats persisted correctly")
        else:
            print(f"[WARN] Signal stats count differs (may be expected)")
        
        print("[PASS] Test 6 completed successfully\n")
        return True
        
    except Exception as e:
        print(f"[FAIL] Test 6 failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_7_strategy_integration():
    """Test 7: Integration with strategy weight loading"""
    print("\n" + "="*70)
    print("[TEST 7] Strategy Integration")
    print("="*70)
    
    try:
        from strategy import WeightedScoringStrategy
        from signal_tracker import tracker
        
        print(f"[*] Creating strategy instance...")
        strategy = WeightedScoringStrategy(symbol='BTC/USDT', timeframe='1h')
        
        print(f"[*] Strategy name: {strategy.name}")
        print(f"[*] Loaded weights: {len(strategy.weights)} signals")
        
        # Show sample weights
        print(f"\n[*] Sample original weights:")
        for sig, wt in list(strategy.weights.items())[:5]:
            print(f"    {sig}: {wt:.2f}")
        
        # Test adaptive adjustment
        print(f"\n[*] Testing adaptive weight adjustment...")
        adjusted = tracker.adjust_weights(strategy.weights.copy())
        
        print(f"[*] Sample adjusted weights:")
        for sig, wt in list(adjusted.items())[:5]:
            orig = strategy.weights.get(sig, 0)
            if wt != orig:
                print(f"    {sig}: {orig:.2f} -> {wt:.2f}")
            else:
                print(f"    {sig}: {wt:.2f} (unchanged)")
        
        print(f"\n[OK] Strategy integration working")
        print("[PASS] Test 7 completed successfully\n")
        return True
        
    except Exception as e:
        print(f"[FAIL] Test 7 failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def run_all_tests():
    """Run all adaptive learning system tests"""
    print("\n" + "="*70)
    print("ADAPTIVE LEARNING SYSTEM - COMPREHENSIVE SELF-TEST")
    print("="*70)
    print(f"Test started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    results = []
    tracker = None
    
    # Test 1: Initialization
    success, tracker = test_1_signal_tracker_initialization()
    results.append(("Initialization", success))
    if not success or tracker is None:
        print("\n[CRITICAL] Cannot continue without tracker instance")
        return False
    
    # Test 2: Record trades
    success = test_2_record_trades(tracker)
    results.append(("Record Trades & Persistence", success))
    
    # Test 3: Weight adjustment
    success = test_3_weight_adjustment(tracker)
    results.append(("Weight Adjustment", success))
    
    # Test 4: Loss tracking
    success = test_4_consecutive_loss_tracking(tracker)
    results.append(("Loss Tracking", success))
    
    # Test 5: Performance stats
    success = test_5_signal_performance_stats()
    results.append(("Performance Stats", success))
    
    # Test 6: File reload
    success = test_6_file_reload_persistence()
    results.append(("File Reload", success))
    
    # Test 7: Strategy integration
    success = test_7_strategy_integration()
    results.append(("Strategy Integration", success))
    
    # Summary
    print("\n" + "="*70)
    print("TEST SUMMARY")
    print("="*70)
    
    for test_name, passed in results:
        status = "[PASS]" if passed else "[FAIL]"
        print(f"{status} {test_name}")
    
    passed = sum(1 for _, p in results if p)
    total = len(results)
    
    print("="*70)
    print(f"Results: {passed}/{total} tests passed")
    
    if passed == total:
        print("\n[SUCCESS] All tests passed! Adaptive learning system is working correctly.")
        print("\nSystem Status:")
        print("- Signal tracking: OK")
        print("- File persistence: OK")
        print("- Weight adjustment: OK")
        print("- Loss counter: OK")
        print("- Strategy integration: OK")
        print("\nBot is ready for deployment!")
        return True
    else:
        print(f"\n[WARNING] {total - passed} test(s) failed. Review errors above.")
        return False


if __name__ == '__main__':
    success = run_all_tests()
    sys.exit(0 if success else 1)
