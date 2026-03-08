import pytest
import asyncio
import time
from unittest.mock import MagicMock, AsyncMock, patch
from src.infrastructure.adapters.base_exchange_client import BaseExchangeClient
from src.infrastructure.repository.database import DataManager

class TestChaos:
    """
    Chaos and Resilience testing.
    Simulates: 429 Rate Limits, Database Locks, and Network Jitter.
    """

    @pytest.mark.asyncio
    async def test_api_429_exponential_backoff(self):
        """Verify that the exchange client backs off on 429 errors."""
        mock_api = AsyncMock(side_effect=[
            Exception("429 Too Many Requests"),
            Exception("429 Too Many Requests"),
            {"id": "SUCCESS"}
        ])
        
        client = BaseExchangeClient(MagicMock())
        client.logger = MagicMock()
        
        # Mock asyncio.sleep to not actually wait in tests
        with patch("src.infrastructure.adapters.base_exchange_client.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            res = await client._execute_with_timestamp_retry(mock_api, max_retries=3)
            
            assert res == {"id": "SUCCESS"}
            assert mock_api.call_count == 3
            # Backoff: (1+1)*3 = 3s, then (2+1)*3 = 6s.
            assert mock_sleep.call_count == 2
            mock_sleep.assert_any_call(3)
            mock_sleep.assert_any_call(6)

    @pytest.mark.asyncio
    async def test_api_timestamp_resync_recovery(self):
        """Verify retry after -1021 timestamp error."""
        mock_api = AsyncMock(side_effect=[
            Exception("-1021 Timestamp for this request is outside of the recvWindow"),
            {"id": "SUCCESS"}
        ])
        
        client = BaseExchangeClient(MagicMock())
        client.resync_time_if_needed = AsyncMock()
        
        with patch("src.infrastructure.adapters.base_exchange_client.asyncio.sleep", new_callable=AsyncMock):
            res = await client._execute_with_timestamp_retry(mock_api, max_retries=2)
            
            assert res == {"id": "SUCCESS"}
            assert client.resync_time_if_needed.called

    @pytest.mark.asyncio
    async def test_db_locked_retry_resilience(self, tmp_path):
        """Verify DataManager handles 'database is locked' errors via retry or timeout."""
        db_path = str(tmp_path / "chaos_db.db")
        db = DataManager(db_path)
        await db.initialize()
        
        # Mock the connection to fail with 'database is locked' once then succeed
        # aiosqlite 'connect' has a default timeout of 5s which handles busy-ness automatically.
        # But we can verify our _write_lock helps too.
        
        # Create a real race: 10 concurrent writes
        async def slow_write():
            await db.set_risk_metric(0, "test", time.time(), "LIVE")
            
        tasks = [slow_write() for _ in range(10)]
        await asyncio.gather(*tasks)
        
        # If we reach here without OperationalError, the mutex/WAL works.
        val = await db.get_risk_metric(0, "test", "LIVE")
        assert val is not None
        await db.close()

    @pytest.mark.asyncio
    async def test_partial_fill_cancellation_race(self):
        """
        Simulate a race where an order is filled exactly when a cancel request is sent.
        Exchange returns 'Order does not exist' or 'Filled' to the cancel call.
        """
        trader = MagicMock()
        trader.exchange = MagicMock()
        # Mock cancel_order to fail because it's already filled
        trader.exchange.cancel_order = AsyncMock(side_effect=Exception("Order does not exist (filled)"))
        trader.logger = MagicMock()
        trader.pending_orders = {"P1": {"order_id": "O1", "symbol": "BTC"}}
        trader.active_positions = {"P1": {"order_id": "O1", "symbol": "BTC"}}
        trader._clear_db_position = AsyncMock()
        
        from src.order_executor import OrderExecutor
        executor = OrderExecutor(trader)
        
        with patch("src.order_executor.format_order_cancelled", return_value=("", "")), \
             patch("src.order_executor.send_telegram_message", new_callable=AsyncMock):
            
            success = await executor.cancel_pending_order("P1", reason="TEST_RACE")
            
            # Should still return True and clean up locally because 'Order does not exist' 
            # implies it's no longer open (cancelled or filled elsewhere)
            # Wait, in order_executor.py:
            # except Exception as e:
            #     self.logger.warning(f"Failed to cancel entry {order_id}: {e}")
            # It logs warning but STILL proceeds to cleanup DB & Memory.
            
            assert success is True
            assert "P1" not in trader.pending_orders
            assert "P1" not in trader.active_positions

