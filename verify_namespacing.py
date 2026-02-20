import sys
import os
import asyncio
from unittest.mock import MagicMock

# Add src to path
sys.path.append(os.path.join(os.getcwd(), 'src'))

from execution import Trader

async def test_namespacing():
    mock_exchange = MagicMock()
    mock_exchange.name = 'BINANCE'
    mock_exchange.can_trade = True
    
    trader = Trader(mock_exchange, dry_run=True)
    
    # Test 1: Key Generation
    symbol = "BTC/USDT"
    tf = "1h"
    key = trader._get_pos_key(symbol, tf)
    print(f"Test 1: {symbol}_{tf} -> {key}")
    assert key == "BINANCE_BTC_USDT_1h"
    
    # Test 2: Key Parsing
    ex, sym, t = trader._parse_pos_key(key)
    print(f"Test 2: {key} -> {ex}, {sym}, {t}")
    assert ex == "BINANCE"
    assert sym == "BTC/USDT"
    assert t == "1h"
    
    # Test 3: Key Generation with settlement
    symbol2 = "ADA/USDT:USDT"
    key2 = trader._get_pos_key(symbol2, tf)
    print(f"Test 3: {symbol2} -> {key2}")
    assert key2 == "BINANCE_ADA_USDT_1h"
    
    # Test 4: Migration Logic
    # Mock the class method to control what __init__ loads
    original_load = Trader._load_positions
    Trader._load_positions = MagicMock(return_value={
        "ETH/USDT_1h": {"qty": 1},
        "BINANCE_XRP/USDT_4h": {"qty": 10}
    })
    
    try:
        trader2 = Trader(mock_exchange, dry_run=True)
        print("Test 4: Migrated keys:")
        for k in trader2.active_positions:
            print(f"  - {k}")
        
        assert "BINANCE_ETH_USDT_1h" in trader2.active_positions
        assert "BINANCE_XRP_USDT_4h" in trader2.active_positions
        # Recovery check: ETH/USDT -> ETH
        # Wait, if original key was ETH/USDT_1h, rest_of_key.split('_')[0] is ETH/USDT
        # So val['symbol'] becomes ETH/USDT
        assert trader2.active_positions["BINANCE_ETH_USDT_1h"]["symbol"] == "ETH/USDT"
    finally:
        Trader._load_positions = original_load
    
    print("✅ Namespacing verification successful!")

if __name__ == "__main__":
    asyncio.run(test_namespacing())
