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

class TestSyncLogic:
    @pytest.fixture(autouse=True)
    async def setup(self):
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
        
        async def mock_execute(func, *args, **kwargs):
            return await func(*args, **kwargs)
        self.trader._execute_with_timestamp_retry.side_effect = mock_execute
        
        # Mock infer_exit_reason: Use a real instance of BybitAdapter to get a bound method
        from src.infrastructure.adapters.bybit_adapter import BybitAdapter
        adapter = BybitAdapter(self.mock_exchange)
        self.trader.exchange.infer_exit_reason = adapter.infer_exit_reason

        yield
        
        # Cleanup
        await DataManager.clear_instances()

    @pytest.mark.asyncio
    async def test_sync_detects_and_resolves_ghost(self):
        """Test that sync_with_exchange identifies a missing position and resolves it."""
        symbol = 'BTC/USDT'
        pos_key = f"P{self.trader.profile_id}_{self.trader.exchange_name}_{symbol.replace('/', '_')}_1h"
        
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

        # 3. Run Sync
        await self.trader.sync_with_exchange()

        # 4. Verify results
        self.trader._clear_db_position.assert_called_once()
        call_args = self.trader._clear_db_position.call_args[1]
        assert call_args['exit_price'] == 55050.0
        assert call_args['exit_reason'] == 'TP'
        
        self.trader.remove_position.assert_called_once()

    @pytest.mark.asyncio
    async def test_sync_updates_pending_to_filled(self):
        """Test that sync updates an externally filled pending order to ACTIVE."""
        symbol = 'BTC/USDT'
        pos_key = f"P1_BYBIT_BTC_USDT_1h"
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
        assert self.trader.active_positions[pos_key]['status'] == 'filled'
        assert self.trader.active_positions[pos_key]['entry_price'] == 49005.0
        self.trader._update_db_position.assert_called_once_with(pos_key)

    @pytest.mark.asyncio
    async def test_deep_history_sync(self):
        """Test that deep_history_sync identifies a closed trade for a DB-active position."""
        symbol = 'ETH/USDT'
        stale_db_trade = {
            'id': 201,
            'symbol': symbol,
            'side': 'SELL',
            'entry_price': 3000.0,
            'entry_time': int(time.time() * 1000) - 7200000, # 2 hours ago
            'qty': 0.5,
            'status': 'ACTIVE',
            'pos_key': f"P1_BYBIT_ETH_USDT_1h"
        }
        
        # Mock DB returning this stale trade
        self.mock_db.get_active_positions = AsyncMock(return_value=[stale_db_trade])
        
        # Mock exchange history returning the exit (Buy back)
        exit_trade = {
            'symbol': symbol,
            'side': 'buy',
            'price': 2950.0,
            'amount': 0.5,
            'timestamp': int(time.time() * 1000) - 1800000 # 30 mins ago
        }
        self.mock_exchange.fetch_my_trades = AsyncMock(return_value=[exit_trade])
        
        # Run deep sync
        await self.trader.deep_history_sync(lookback_hours=24)
        
        # Verify it cleared DB and tried to remove from memory
        self.trader._clear_db_position.assert_called_once()
        self.trader.remove_position.assert_called_once()
        
        # Check params
        call_args = self.trader._clear_db_position.call_args[1]
        assert call_args['exit_price'] == 2950.0
        assert call_args['exit_reason'] == 'TP'

    @pytest.mark.asyncio
    async def test_resolve_ghost_uses_bybit_stoploss_field(self):
        """Verify Bybit stopOrderType='StopLoss' results in 'SL' reason."""
        symbol = 'ATOM/USDT'
        pos_key = f"P1_BYBIT_ATOM_USDT_1h"
        self.trader.active_positions = {
            pos_key: {
                'symbol': symbol, 'side': 'SELL', 'status': 'filled', 
                'entry_price': 1.803, 'sl': 1.85, 'timestamp': int(time.time() * 1000) - 3600000
            }
        }
        
        self.mock_exchange.fetch_positions = AsyncMock(return_value=[])
        self.mock_exchange.fetch_open_orders = AsyncMock(return_value=[])
        exit_trade = {
            'symbol': symbol, 'side': 'buy', 'price': 1.85, 'amount': 10.0,
            'timestamp': int(time.time() * 1000) - 60000,
            'info': {'stopOrderType': 'StopLoss'}
        }
        self.mock_exchange.fetch_my_trades = AsyncMock(return_value=[exit_trade])
        self.trader.set_sl_cooldown = AsyncMock()

        await self.trader.sync_with_exchange()
        
        # Verify
        self.trader._clear_db_position.assert_called_once()
        call_args = self.trader._clear_db_position.call_args[1]
        assert call_args['exit_reason'] == 'SL'
        self.trader.set_sl_cooldown.assert_called_once_with(symbol)

    @pytest.mark.asyncio
    async def test_resolve_ghost_proximity_check(self):
        """Verify proximity check when exchange info is missing."""
        symbol = 'BTC/USDT'
        pos_key = f"P1_BYBIT_BTC_USDT_1h"
        self.trader.active_positions = {
            pos_key: {
                'symbol': symbol, 'side': 'BUY', 'status': 'filled', 
                'entry_price': 50000.0, 'sl': 49000.0, 'tp': 55000.0,
                'timestamp': int(time.time() * 1000) - 3600000
            }
        }
        
        self.mock_exchange.fetch_positions = AsyncMock(return_value=[])
        self.mock_exchange.fetch_open_orders = AsyncMock(return_value=[])
        # Exit price is close to SL
        exit_trade = {
            'symbol': symbol, 'side': 'sell', 'price': 49010.0, 'amount': 0.1,
            'timestamp': int(time.time() * 1000) - 60000,
            'info': {} # Empty info
        }
        self.mock_exchange.fetch_my_trades = AsyncMock(return_value=[exit_trade])
        self.trader.set_sl_cooldown = AsyncMock()

        await self.trader.sync_with_exchange()
        
        call_args = self.trader._clear_db_position.call_args[1]
        assert call_args['exit_reason'] == 'SL'
        self.trader.set_sl_cooldown.assert_called_once_with(symbol)

    @pytest.mark.asyncio
    async def test_infer_exit_entry_price_zero(self):
        """Verify fix for SL classification when entry_price is 0."""
        symbol = 'ETH/USDT'
        pos = {
            'symbol': symbol, 'side': 'BUY', 'status': 'filled', 
            'entry_price': 0, 'sl': 2000.0, 'tp': 2500.0
        }
        trade = {
            'price': 2005.0,
            'info': {}
        }
        reason = self.trader._infer_exit_reason(trade, pos)
        assert reason == 'SL'
