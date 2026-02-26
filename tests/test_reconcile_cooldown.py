import unittest
from unittest.mock import MagicMock, AsyncMock, patch
import asyncio
import sys
import os
import inspect

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from execution import Trader

class TestTraderCooldownLogic(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Create a mock exchange
        self.mock_exchange = MagicMock()
        self.mock_exchange.name = 'BYBIT'
        self.mock_exchange.can_trade = True
        self.mock_exchange.is_public_only = False
        self.mock_exchange.milliseconds = MagicMock(return_value=1000000000)
        
        # Initialize Trader in LIVE mode (dry_run=False)
        self.mock_db = MagicMock()
        self.trader = Trader(self.mock_exchange, db=self.mock_db, profile_id=1, dry_run=False)
        self.trader.logger = MagicMock()
        
        # Ensure cooldowns file operations are mocked entirely to prevent disk I/O side effects during tests
        self.trader._load_cooldowns = MagicMock()
        self.trader._save_cooldowns = MagicMock()
        
        # We want to spy on set_sl_cooldown, but call the actual implementation
        self.original_set_sl_cooldown = self.trader.set_sl_cooldown
        self.trader._async_save_cooldowns = AsyncMock()
        self.trader.set_sl_cooldown = AsyncMock(side_effect=self.original_set_sl_cooldown)

        # Mock standard execution methods
        self.trader.log_trade = AsyncMock()
        self.trader._save_positions = MagicMock()
        self.trader.exchange_name = 'BYBIT'

    async def run_reconciliation_test(self, side, entry_price, sl_price, exit_price):
        """Helper to simulate the missing position scenario in reconcile_positions."""
        symbol = 'BTC/USDT'
        # IMPORTANT: Pos key format used by real bot is: e.g. BYBIT_BTC_USDT_1h
        pos_key = f"BYBIT_{symbol.replace('/', '_')}_1h"
        
        # 1. Setup Active Position
        self.trader.active_positions = {
            pos_key: {
                'symbol': symbol,
                'side': side,
                'entry_price': entry_price,
                'sl': sl_price,
                'qty': 1.0,
                'status': 'filled',
                'missing_cycles': 4
            }
        }
        self.trader.pending_orders = {}
        self.trader._missing_order_counts = {}

        # 2. Mock Exchange API Responses
        # Exchange returns no open positions (meaning it closed)
        self.mock_exchange.fetch_positions = AsyncMock(return_value=[])
        self.mock_exchange.fetch_open_orders = AsyncMock(return_value=[])
        
        # Mock the trade history returning the exit price
        self.mock_exchange.fetch_my_trades = AsyncMock(return_value=[
            {
                'symbol': symbol,
                'side': 'sell' if side == 'BUY' else 'buy',
                'price': exit_price,
                'amount': 1.0,
                'timestamp': 1000000000
            }
        ])

        # Overwrite retry mechanism to avoid sleeping during tests
        self.trader._execute_with_timestamp_retry = AsyncMock()
        
        # Override the specific calls in reconcile_positions
        async def mock_execute(func, *args, **kwargs):
            try:
                if inspect.iscoroutinefunction(func) or isinstance(func, AsyncMock):
                    return await func(*args, **kwargs)
                else:
                    return func(*args, **kwargs)
            except Exception as e:
                print(f"Exception in mock_execute: {e}")
                raise e
        self.trader._execute_with_timestamp_retry.side_effect = mock_execute

        # Print suppression during test removed for debugging
        # Run the reconciliation
        print(f"Running test for side {side}, entry {entry_price}, exit {exit_price}")
        
        # Output all warnings from logger
        self.trader.logger.warning = lambda msg: print(f"WARNING: {msg}")
        self.trader.logger.error = lambda msg: print(f"ERROR: {msg}")
        self.trader.logger.info = lambda msg: print(f"INFO: {msg}")

        print(f"Pre-Reconcile Active pos keys: {list(self.trader.active_positions.keys())}")
        print(f"Self Exchange Name: {self.trader.exchange_name}")

        await self.trader.reconcile_positions(auto_fix=True, force_verify=True)
        
        print(f"Post-Reconcile Active pos keys: {list(self.trader.active_positions.keys())}")

    async def test_cooldown_triggered_on_clear_loss(self):
        # BUY at 60000, SL at 59000. Exited at 58500 (Loss)
        await self.run_reconciliation_test(side='BUY', entry_price=60000.0, sl_price=59000.0, exit_price=58500.0)
        self.trader.set_sl_cooldown.assert_called_once_with('BTC/USDT')

    async def test_cooldown_triggered_on_short_clear_loss(self):
        # SELL at 60000, SL at 61000. Exited at 61500 (Loss)
        await self.run_reconciliation_test(side='SELL', entry_price=60000.0, sl_price=61000.0, exit_price=61500.0)
        self.trader.set_sl_cooldown.assert_called_once_with('BTC/USDT')

    async def test_cooldown_triggered_on_profitable_sl_hit(self):
        # BUY at 60000, SL trailed to 65000. Exited at 64900 (Profit, but very close to SL!)
        # Difference = abs(64900 - 65000) / 65000 = 100 / 65000 = 0.0015 (< 0.005)
        await self.run_reconciliation_test(side='BUY', entry_price=60000.0, sl_price=65000.0, exit_price=64900.0)
        self.trader.set_sl_cooldown.assert_called_once_with('BTC/USDT')

    async def test_cooldown_NOT_triggered_on_clear_tp(self):
        # BUY at 60000, SL at 59000, TP hit at 68000.
        # It's a clear profit and > 0.5% away from SL.
        await self.run_reconciliation_test(side='BUY', entry_price=60000.0, sl_price=59000.0, exit_price=68000.0)
        self.trader.set_sl_cooldown.assert_not_called()


if __name__ == '__main__':
    unittest.main()
