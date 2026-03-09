import pytest
from unittest.mock import MagicMock, AsyncMock, patch
import asyncio
import sys
import os
import time

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from src.execution import Trader
from src.infrastructure.repository.database import DataManager

class TestFastSLSync:
    @pytest.fixture(autouse=True)
    async def setup(self):
        # Create a mock exchange
        self.mock_exchange = MagicMock()
        self.mock_exchange.name = 'BINANCE'
        self.mock_exchange.can_trade = True
        self.mock_exchange.is_public_only = False
        self.mock_exchange.milliseconds = MagicMock(return_value=int(time.time() * 1000))
        
        # Initialize Trader
        self.mock_db = MagicMock()
        self.trader = Trader(self.mock_exchange, db=self.mock_db, profile_id=1, dry_run=False)
        self.trader.logger = MagicMock()
        self.trader.exchange_name = 'BINANCE'
        
        # Mock standard methods
        self.trader.log_trade = AsyncMock()
        self.trader._save_positions = MagicMock()
        self.trader._load_positions = MagicMock()
        self.trader.set_sl_cooldown = AsyncMock()

        yield
        await DataManager.clear_instances()

    @pytest.mark.asyncio
    async def test_fast_sl_recovery(self):
        """Test Case: Pending order is gone from open, but filled and closed in history."""
        symbol = 'BTC/USDT'
        order_id = '12345'
        pos_key = f"BINANCE_BTC_USDT_adopted"
        
        # 1. Setup Active Position (Status: Pending)
        self.trader.active_positions = {
            pos_key: {
                'symbol': symbol,
                'side': 'BUY',
                'entry_price': 50000.0,
                'qty': 0.1,
                'status': 'pending',
                'order_id': order_id,
                'timestamp': int(time.time() * 1000) - 10000
            }
        }
        
        # 2. Mock Exchange Responses
        self.mock_exchange.fetch_positions = AsyncMock(return_value=[])
        self.mock_exchange.fetch_open_orders = AsyncMock(return_value=[])
        
        # Trade history contains both ENTRY and EXIT (SL hit)
        self.mock_exchange.fetch_my_trades = AsyncMock(return_value=[
            {
                'id': 'trade_1',
                'order': order_id,
                'symbol': symbol,
                'side': 'buy',
                'price': 50000.0,
                'amount': 0.1,
                'timestamp': int(time.time() * 1000) - 5000
            },
            {
                'id': 'trade_2',
                'order': 'sl_order_id_678', # Different ID because SL is a separate order usually
                'symbol': symbol,
                'side': 'sell',
                'price': 49000.0,
                'amount': 0.1,
                'timestamp': int(time.time() * 1000) - 2000
            }
        ])

        # Overwrite retry mechanism
        self.trader._execute_with_timestamp_retry = AsyncMock()
        async def mock_execute(func, *args, **kwargs):
            return await func(*args, **kwargs)
        self.trader._execute_with_timestamp_retry.side_effect = mock_execute

        # 3. Run reconciliation
        await self.trader.reconcile_positions(force_verify=True)
        
        # 4. Verifications
        self.trader.log_trade.assert_called_once()
        args, kwargs = self.trader.log_trade.call_args
        assert args[0] == pos_key
        assert args[1] == 49000.0
        assert kwargs['exit_reason'] == "Exchange Sync (Fast Fill + SL)"
        
        # Cooldown should be applied because it's a loss
        self.trader.set_sl_cooldown.assert_called_once_with(symbol)
        
        # Position should be removed from active_positions
        assert pos_key not in self.trader.active_positions
