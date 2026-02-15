"""
Automated Self-Test System for Trading Bot
Validates configuration, connectivity, and system integrity.
Run this after any code changes to ensure system health.
"""
import sys
import os
import json
import asyncio
from datetime import datetime

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

class SelfTest:
    def __init__(self):
        self.results = []
        self.passed = 0
        self.failed = 0
        
    def log_test(self, test_name, passed, message=""):
        """Log a test result."""
        status = "✅ PASS" if passed else "❌ FAIL"
        self.results.append((test_name, passed, message))
        if passed:
            self.passed += 1
        else:
            self.failed += 1
        print(f"{status} | {test_name}")
        if message:
            print(f"      {message}")
    
    def test_env_config(self):
        """Test 1: Verify .env and config.py settings."""
        print("\n" + "="*70)
        print("[TEST 1] Environment & Configuration")
        print("="*70)
        
        try:
            from dotenv import load_dotenv
            load_dotenv()
            
            # Check API keys
            binance_key = os.getenv('BINANCE_API_KEY')
            binance_secret = os.getenv('BINANCE_API_SECRET')
            
            if not binance_key or 'your_' in binance_key:
                self.log_test("API Keys", False, "BINANCE_API_KEY not configured in .env")
                return
            
            if not binance_secret or 'your_' in binance_secret:
                self.log_test("API Keys", False, "BINANCE_API_SECRET not configured in .env")
                return
                
            self.log_test("API Keys", True, "Binance API keys found")
            
            # Check config.py
            from config import TRADING_SYMBOLS, TIMEFRAMES, LEVERAGE
            
            if not TRADING_SYMBOLS:
                self.log_test("Trading Symbols", False, "TRADING_SYMBOLS is empty")
                return
                
            self.log_test("Trading Symbols", True, f"{len(TRADING_SYMBOLS)} symbols configured")
            self.log_test("Timeframes", True, f"{len(TIMEFRAMES)} timeframes configured")
            self.log_test("Leverage", True, f"Leverage set to {LEVERAGE}x")
            
        except Exception as e:
            self.log_test("Environment Config", False, str(e))
    
    async def test_exchange_connectivity(self):
        """Test 2: Test exchange connectivity and time synchronization."""
        print("\n" + "="*70)
        print("[TEST 2] Exchange Connectivity & Time Sync")
        print("="*70)
        
        try:
            from data_manager import MarketDataManager
            
            manager = MarketDataManager()
            
            # Test time sync
            sync_result = await manager.sync_server_time()
            self.log_test("Time Synchronization", sync_result, 
                         f"Server time offset configured")
            
            # Test basic API call
            try:
                ticker = await manager.fetch_ticker('BTC/USDT')
                if ticker and 'last' in ticker:
                    self.log_test("Exchange API", True, 
                                 f"BTC/USDT price: ${ticker['last']:.2f}")
                else:
                    self.log_test("Exchange API", False, "Invalid ticker response")
            except Exception as e:
                self.log_test("Exchange API", False, str(e))
            
            # Close connection
            await manager.close()
            
        except Exception as e:
            self.log_test("Exchange Connectivity", False, str(e))
    
    def test_strategy_config(self):
        """Test 3: Validate strategy_config.json against TRADING_SYMBOLS."""
        print("\n" + "="*70)
        print("[TEST 3] Strategy Configuration Validation")
        print("="*70)
        
        try:
            from config import TRADING_SYMBOLS, TIMEFRAMES
            
            config_path = os.path.join(os.path.dirname(__file__), '..', 'src', 'strategy_config.json')
            
            if not os.path.exists(config_path):
                self.log_test("Strategy Config File", False, "strategy_config.json not found")
                return
            
            with open(config_path, 'r') as f:
                config = json.load(f)
            
            self.log_test("Strategy Config File", True, "File loaded successfully")
            
            # Check for unauthorized symbols
            from config import BINANCE_SYMBOLS, BYBIT_SYMBOLS
            exchange_symbols_map = {
                'BINANCE': BINANCE_SYMBOLS,
                'BYBIT': BYBIT_SYMBOLS
            }
            unauthorized_configs = []
            
            for key in config.keys():
                parts = key.split('_')
                if len(parts) >= 3:
                    ex_name = parts[0]
                    symbol = parts[1]
                    tf = parts[2]
                    
                    allowed = exchange_symbols_map.get(ex_name, [])
                    if symbol not in allowed:
                        unauthorized_configs.append(f"{ex_name} {symbol}")
                elif '_' in key:
                    # Fallback for old style keys (just symbol_tf)
                    symbol = key.split('_')[0]
                    if symbol not in TRADING_SYMBOLS:
                        unauthorized_configs.append(f"UNKNOWN {symbol}")
            
            if unauthorized_configs:
                self.log_test("Symbol Adherence", False, 
                             f"Unauthorized configurations found: {unauthorized_configs[:3]}...")
            else:
                self.log_test("Symbol Adherence", True, 
                             "All configs use authorized exchange/symbol pairs")
            
            # Count enabled configs
            enabled_count = sum(1 for v in config.values() if isinstance(v, dict) and v.get('enabled', False))
            self.log_test("Enabled Configs", True, f"{enabled_count} configurations enabled")
            
        except Exception as e:
            self.log_test("Strategy Config", False, str(e))
    
    def test_positions_integrity(self):
        """Test 4: Check positions.json integrity."""
        print("\n" + "="*70)
        print("[TEST 4] Position Data Integrity")
        print("="*70)
        
        try:
            positions_file = os.path.join(os.path.dirname(__file__), '..', 'src', 'positions.json')
            
            if not os.path.exists(positions_file):
                self.log_test("Positions File", True, "No positions.json (clean state)")
                return
            
            with open(positions_file, 'r') as f:
                positions = json.load(f)
            
            self.log_test("Positions File", True, f"{len(positions)} positions loaded")
            
            # Validate structure
            for key, pos in positions.items():
                required_fields = ['symbol', 'side', 'qty', 'entry_price']
                missing = [f for f in required_fields if f not in pos]
                if missing:
                    self.log_test(f"Position {key}", False, f"Missing fields: {missing}")
                    return
            
            self.log_test("Position Structure", True, "All positions have required fields")
            
        except json.JSONDecodeError as e:
            self.log_test("Positions File", False, f"JSON decode error: {e}")
        except Exception as e:
            self.log_test("Positions File", False, str(e))
    
    def test_module_imports(self):
        """Test 5: Verify all critical modules can be imported."""
        print("\n" + "="*70)
        print("[TEST 5] Module Import Validation")
        print("="*70)
        
        modules = [
            'config',
            'data_manager',
            'execution',
            'strategy',
            'feature_engineering',
            'analyzer',
            'base_exchange_client'
        ]
        
        for module_name in modules:
            try:
                __import__(module_name)
                self.log_test(f"Import {module_name}", True)
            except Exception as e:
                self.log_test(f"Import {module_name}", False, str(e))
    
    def print_summary(self):
        """Print final test summary."""
        print("\n" + "="*70)
        print("SELF-TEST SUMMARY")
        print("="*70)
        
        total = self.passed + self.failed
        pass_rate = (self.passed / total * 100) if total > 0 else 0
        
        print(f"\nTotal Tests: {total}")
        print(f"✅ Passed: {self.passed}")
        print(f"❌ Failed: {self.failed}")
        print(f"Pass Rate: {pass_rate:.1f}%")
        
        if self.failed == 0:
            print("\n🎉 ALL TESTS PASSED! System is healthy.")
            return True
        else:
            print(f"\n⚠️  {self.failed} TEST(S) FAILED. Review errors above.")
            return False

async def main():
    """Run all self-tests."""
    print("="*70)
    print("TRADING BOT SELF-TEST SYSTEM")
    print("="*70)
    print(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    
    tester = SelfTest()
    
    # Run tests
    tester.test_env_config()
    await tester.test_exchange_connectivity()
    tester.test_strategy_config()
    tester.test_positions_integrity()
    tester.test_module_imports()
    
    # Print summary
    success = tester.print_summary()
    
    return 0 if success else 1

if __name__ == '__main__':
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
