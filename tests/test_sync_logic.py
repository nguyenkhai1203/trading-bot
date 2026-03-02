import unittest
from unittest.mock import MagicMock, AsyncMock, patch
import asyncio
import sys
import os
import time

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from execution import Trader

class TestSyncLogic(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Create a mock exchange
        self.mock_exchange = MagicMock()
        self.mock_exchange.name = 'BYBIT'
        self.mock_exchange.can_trade = True
        self.mock_exchange.milliseconds = MagicMock(return_value=int(time.time() * 1000))
        
        # Mock database
        self.mock_db = AsyncMock()
        
        # Initialize Trader in LIVE mode (sync only runs in live)
        self.trader = Trader(self.mock_exchange, db=self.mock_db, profile_id=1, dry_run=False)
        self.trader.logger = MagicMock()
        self.trader.exchange_name = 'BYBIT'
        
        # Mock standard execution methods
        self.trader.log_trade = AsyncMock()
        self.trader._clear_db_position = AsyncMock()
        self.trader.remove_position = AsyncMock()
        self.trader._update_db_position = AsyncMock()
        self.trader._execute_with_timestamp_retry = AsyncMock()

    async def test_sync_detects_and_resolves_ghost(self):
        """Test that sync_with_exchange identifies a missing position and resolves it."""
        symbol = 'BTC/USDT'
        pos_key = f"BYBIT_{symbol.replace('/', '_')}_1h"
        
        # 1. Setup Active Position in memory
        sl_order_id = 'sl_123'
        tp_order_id = 'tp_789'
        self.trader.active_positions = {
            pos_key: {
                'id': 101,
                'symbol': symbol,
                'side': 'BUY',
                'entry_price': 50000.0,
                'qty': 1.0,
                'status': 'filled',
                'tp': 55000.0,
                'sl': 48000.0,
                'sl_order_id': sl_order_id,
                'tp_order_id': tp_order_id,
                'timestamp': int(time.time() * 1000) - 3600000 # 1 hour ago
            }
        }

        # 2. Mock Exchange API Responses
        # Exchange returns no open positions (meaning it closed)
        self.mock_exchange.fetch_positions = AsyncMock(return_value=[])
        self.mock_exchange.fetch_open_orders = AsyncMock(return_value=[])
        
        # Mock ticker showing price crossed TP
        self.mock_exchange.fetch_ticker = AsyncMock(return_value={'last': 56000.0})
        
        # Mock trade history returning the exit trade
        exit_trade = {
            'symbol': symbol,
            'side': 'sell',
            'price': 55050.0,
            'amount': 1.0,
            'timestamp': int(time.time() * 1000) - 60000 # 1 minute ago
        }
        self.mock_exchange.fetch_my_trades = AsyncMock(return_value=[exit_trade])

        # Helper for retry wrapper
        async def mock_execute(func, *args, **kwargs):
            return await func(*args, **kwargs)
        self.trader._execute_with_timestamp_retry.side_effect = mock_execute

        # 3. Run Sync
        await self.trader.sync_with_exchange()

        # 4. Verify results
        # Should have called _resolve_ghost_position -> _clear_db_position -> remove_position
        self.trader._clear_db_position.assert_called_once()
        # Verify exit price was captured correctly from history
        call_args = self.trader._clear_db_position.call_args[1]
        self.assertEqual(call_args['exit_price'], 55050.0)
        self.assertIn("SYNC", call_args['exit_reason'])
        
        self.trader.remove_position.assert_called_once()
        # Verify position was removed from memory if remove_position logic was called
        # (Since we mocked remove_position, we just check if it was called)

    async def test_sync_updates_pending_to_filled(self):
        """Test that sync updates an externally filled pending order to ACTIVE."""
        symbol = 'BTC/USDT'
        pos_key = f"BYBIT_{symbol.replace('/', '_')}_1h"
        order_id = "order_123"
        
        # 1. Setup Pending Position
        self.trader.active_positions = {
            pos_key: {
                'id': 102,
                'symbol': symbol,
                'side': 'BUY',
                'entry_price': 49000.0,
                'qty': 1.0,
                'status': 'pending',
                'order_id': order_id
            }
        }

        # 2. Mock Exchange API Responses
        # Order is gone from open orders
        self.mock_exchange.fetch_open_orders = AsyncMock(return_value=[])
        # But symbol exists in active positions on exchange
        self.mock_exchange.fetch_positions = AsyncMock(return_value=[
            {
                'symbol': 'BTCUSDT',
                'side': 'Buy',
                'contracts': 1.0,
                'entryPrice': 49005.0
            }
        ])
        
        # Mock ticker
        self.mock_exchange.fetch_ticker = AsyncMock(return_value={'last': 49100.0})

        # 3. Run Sync
        await self.trader.sync_with_exchange()

        # 4. Verify results
        self.assertEqual(self.trader.active_positions[pos_key]['status'], 'filled')
        self.assertEqual(self.trader.active_positions[pos_key]['entry_price'], 49005.0)
        self.trader._update_db_position.assert_called_once_with(pos_key)

if __name__ == '__main__':
    unittest.main()
