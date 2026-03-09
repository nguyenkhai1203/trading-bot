import pytest
from unittest.mock import MagicMock, AsyncMock, patch
import asyncio
import sys
import os
import inspect
import time

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from src.execution import Trader
from src.infrastructure.repository.database import DataManager

class TestTraderCooldownLogic:
    @pytest.fixture(autouse=True)
    async def setup(self):
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
        
        # Ensure cooldowns file operations are mocked entirely
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
        
        # Mock infer_exit_reason to use real Bybit logic with bound method
        from src.infrastructure.adapters.bybit_adapter import BybitAdapter
        adapter = BybitAdapter(self.mock_exchange)
        self.mock_exchange.infer_exit_reason = adapter.infer_exit_reason
        
        yield
        
        # Cleanup
        await DataManager.clear_instances()

    async def run_reconciliation_test(self, side, entry_price, sl_price, exit_price):
        """Helper to simulate the missing position scenario in reconcile_positions."""
        symbol = 'BTC/USDT'
        pos_key = f"P{self.trader.profile_id}_{self.mock_exchange.name}_{symbol.replace('/', '_')}_1h"
        
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

        # Overwrite retry mechanism
        self.trader._execute_with_timestamp_retry = AsyncMock()
        
        async def mock_execute(func, *args, **kwargs):
            if inspect.iscoroutinefunction(func) or isinstance(func, AsyncMock):
                return await func(*args, **kwargs)
            else:
                return func(*args, **kwargs)
        self.trader._execute_with_timestamp_retry.side_effect = mock_execute

        await self.trader.reconcile_positions(auto_fix=True, force_verify=True)

    @pytest.mark.asyncio
    async def test_cooldown_triggered_on_clear_loss(self):
        await self.run_reconciliation_test(side='BUY', entry_price=60000.0, sl_price=59000.0, exit_price=58500.0)
        self.trader.set_sl_cooldown.assert_called_once_with('BTC/USDT')

    @pytest.mark.asyncio
    async def test_cooldown_triggered_on_short_clear_loss(self):
        await self.run_reconciliation_test(side='SELL', entry_price=60000.0, sl_price=61000.0, exit_price=61500.0)
        self.trader.set_sl_cooldown.assert_called_once_with('BTC/USDT')

    @pytest.mark.asyncio
    async def test_cooldown_triggered_on_profitable_sl_hit(self):
        await self.run_reconciliation_test(side='BUY', entry_price=60000.0, sl_price=65000.0, exit_price=64900.0)
        self.trader.set_sl_cooldown.assert_called_once_with('BTC/USDT')

    @pytest.mark.asyncio
    async def test_cooldown_NOT_triggered_on_clear_tp(self):
        await self.run_reconciliation_test(side='BUY', entry_price=60000.0, sl_price=59000.0, exit_price=68000.0)
        self.trader.set_sl_cooldown.assert_not_called()
