import json
import os
from datetime import datetime
from collections import defaultdict

# Constants
PERFORMANCE_FILE = 'd:/code/tradingBot/src/signal_performance.json'
DEFAULT_EXCHANGE = 'BINANCE'
LOOKBACK_TRADES = 20

def standardize_symbol(symbol):
    """
    Standardize symbol format to EXCHANGE_BASE_QUOTE.
    e.g. BTC/USDT -> BINANCE_BTC_USDT
    """
    if not symbol:
        return "UNKNOWN"
    
    # Check if already standardized
    if '_' in symbol and any(ex in symbol.upper() for ex in ['BINANCE', 'BYBIT']):
        return symbol.upper()
    
    # Handle BTC/USDT or NEAR/USDT
    base_quote = symbol.split(':')[0].replace('/', '_').upper()
    return f"{DEFAULT_EXCHANGE}_{base_quote}"

def clean_data():
    if not os.path.exists(PERFORMANCE_FILE):
        print(f"❌ File not found: {PERFORMANCE_FILE}")
        return

    print(f"🧹 Cleaning {PERFORMANCE_FILE}...")
    
    with open(PERFORMANCE_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)

    original_count = len(data.get('trades', []))
    
    # 1. Filter out garbage/test trades
    clean_trades = []
    for t in data.get('trades', []):
        signals = t.get('signals', [])
        symbol = t.get('symbol', '')
        
        # Remove synthetic test trades (sig1)
        if 'sig1' in signals:
            continue
            
        # Standardize symbol
        t['symbol'] = standardize_symbol(symbol)
        
        # Cleanup null results/dummy fields
        if t.get('entry_price') is None and t.get('exit_price') is None and 'manual_sync' not in signals:
            continue
            
        clean_trades.append(t)

    # 2. Re-calculate Signal Stats
    new_stats = {}
    for t in clean_trades:
        signals = t.get('signals', [])
        result = t.get('result')
        
        for sig in signals:
            if sig not in new_stats:
                new_stats[sig] = {
                    'total': 0,
                    'wins': 0,
                    'losses': 0,
                    'recent_results': []
                }
            
            stats = new_stats[sig]
            stats['total'] += 1
            if result == 'WIN':
                stats['wins'] += 1
            else:
                stats['losses'] += 1
            
            # Update recent results (1=win, 0=loss)
            stats['recent_results'].append(1 if result == 'WIN' else 0)
            if len(stats['recent_results']) > LOOKBACK_TRADES:
                stats['recent_results'] = stats['recent_results'][-LOOKBACK_TRADES:]

    # Update data object
    data['trades'] = clean_trades
    data['signal_stats'] = new_stats
    
    # Save back
    with open(PERFORMANCE_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)

    removed = original_count - len(clean_trades)
    print(f"✅ Cleanup Complete!")
    print(f"📊 Trades: {original_count} -> {len(clean_trades)} (Removed {removed} test entries)")
    print(f"🔀 All symbols standardized to EXCHANGE_BASE_QUOTE format.")

if __name__ == '__main__':
    clean_data()
