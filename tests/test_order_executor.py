import pytest
import asyncio
import time
from unittest.mock import MagicMock, patch, AsyncMock
from src.order_executor import OrderExecutor

class TestOrderExecutor:
    """
    Refined test suite for OrderExecutor.
    """

    @pytest.fixture
    def mock_trader(self):
        trader = MagicMock()
        trader.exchange = MagicMock()
        trader.db = AsyncMock()
        trader.logger = MagicMock()
        trader.exchange_name = "BINANCE"
        trader.profile_id = 1
        trader.profile_name = "TestProfile"
        trader.account_key = "BINANCE_KEY"
        trader.active_positions = {}
        trader.pending_orders = {}
        trader._get_pos_key.return_value = "P1_BINANCE_BTC_1h"
        trader._normalize_symbol.return_value = "BTC"
        
        # ALL AWAITED METHODS MUST BE AsyncMock
        trader._execute_with_timestamp_retry = AsyncMock()
        trader._update_db_position = AsyncMock()
        trader._clear_db_position = AsyncMock()
        trader.check_margin_error = AsyncMock(return_value=False)
        trader.cancel_pending_order = AsyncMock(return_value=True)
        
        # Mock class attribute access
        class MockTraderClass:
            _shared_account_cache = {"BINANCE_KEY": {'pos_symbols': set(), 'open_order_symbols': set()}}
        trader.__class__ = MockTraderClass
        
        return trader

    @pytest.fixture
    def executor(self, mock_trader):
        # We need to ensure executor.exchange is the same as trader.exchange
        return OrderExecutor(mock_trader)

    @pytest.mark.asyncio
    async def test_place_order_timeout_recovery(self, executor):
        """Verify timeout recovery logic."""
        # 1. create_order fails
        executor.trader._execute_with_timestamp_retry.side_effect = Exception("Timeout")
        
        # 2. fetch_order succeeds
        recovered_order = {'id': 'EX123', 'status': 'open', 'clientOrderId': 'CID1', 'average': 40000}
        executor.exchange.fetch_order = AsyncMock(return_value=recovered_order)
        executor.exchange.is_tpsl_attached_supported.return_value = False
        
        # 3. Mock dependencies
        with patch("src.order_executor.format_pending_order", return_value=("", "")), \
             patch("src.order_executor.send_telegram_message", new_callable=AsyncMock):
            
            res = await executor.place_order(
                symbol="BTC/USDT", timeframe="1h", side="BUY", qty=1.0, price=40000, order_type="limit"
            )
            
            assert res == recovered_order
            assert executor.exchange.fetch_order.called
            assert "P1_BINANCE_BTC_1h" in executor.trader.active_positions

    @pytest.mark.asyncio
    async def test_monitor_limit_order_timeout(self, executor):
        """Verify timeout eviction in monitor loop."""
        pos_data = {'status': 'pending', 'order_id': 'ORD1', 'symbol': 'BTC'}
        executor.trader.active_positions["P1_BINANCE_BTC_1h"] = pos_data
        
        # Bypass throttle and trigger timeout
        with patch("src.order_executor.config.LIMIT_ORDER_TIMEOUT", 0.1), \
             patch("src.order_executor.time.time", side_effect=[0, 1000]), \
             patch("src.order_executor.asyncio.sleep", new_callable=AsyncMock):
            
            await executor.monitor_limit_order_fill("P1_BINANCE_BTC_1h", "ORD1", "BTC")
            
            assert executor.trader.cancel_pending_order.called

    @pytest.mark.asyncio
    async def test_monitor_limit_order_fill_success(self, executor):
        """Verify fill detection in monitor loop."""
        pos_data = {'status': 'pending', 'order_id': 'ORD1', 'symbol': 'BTC', 'qty': 1.0, 'timeframe': '1h', 'side': 'BUY'}
        executor.trader.active_positions["P1_BINANCE_BTC_1h"] = pos_data
        
        # Mock exchange.fetch_order to return filled status
        filled_order = {'status': 'closed', 'filled': 1.0, 'average': 40100, 'id': 'ORD1', 'timestamp': 123456}
        executor.trader._execute_with_timestamp_retry.return_value = filled_order
        
        with patch("src.order_executor.asyncio.sleep", new_callable=AsyncMock), \
             patch("src.order_executor.format_position_filled", return_value=("", "")), \
             patch("src.order_executor.send_telegram_message", new_callable=AsyncMock), \
             patch.object(executor, "create_sl_tp_orders_for_position", new_callable=AsyncMock):
            
            await executor.monitor_limit_order_fill("P1_BINANCE_BTC_1h", "ORD1", "BTC")
            
            assert pos_data['status'] == 'filled'
            assert pos_data['entry_price'] == 40100
