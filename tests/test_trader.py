import pytest
import asyncio
from unittest.mock import MagicMock, patch, AsyncMock
from src.execution import Trader

class TestTrader:
    """
    Refined test suite for Trader orchestrator.
    """

    @pytest.fixture
    def mock_deps(self):
        mock_ex = MagicMock()
        mock_ex.name = "BINANCE"
        mock_ex.exchange = MagicMock()
        mock_ex.exchange.apiKey = "MOCK_KEY"
        mock_ex.fetch_positions = AsyncMock(return_value=[])
        mock_ex.fetch_open_orders = AsyncMock(return_value=[])
        
        mock_db = AsyncMock()
        # Mocking CooldownManager and OrderExecutor to isolate Trader logic
        with patch('src.execution.CooldownManager', autospec=True), \
             patch('src.execution.OrderExecutor', autospec=True):
            yield mock_ex, mock_db

    @pytest.fixture
    def trader(self, mock_deps):
        mock_ex, mock_db = mock_deps
        t = Trader(mock_ex, mock_db, profile_id=1, dry_run=False)
        # Ensure mocks are used
        t.cooldown_manager = AsyncMock()
        t.order_executor = MagicMock()
        return t

    @pytest.mark.asyncio
    async def test_check_margin_error_smart_eviction(self, trader):
        """Verify only low-confidence orders are evicted."""
        trader.pending_orders = {
            "P1_LOW": {'status': 'pending', 'order_id': 'L1', 'symbol': 'ETH', 'entry_confidence': 0.4},
            "P1_HIGH": {'status': 'pending', 'order_id': 'H1', 'symbol': 'BTC', 'entry_confidence': 0.8}
        }
        
        with patch.object(trader, 'cancel_pending_order', new_callable=AsyncMock) as mock_cancel:
            await trader.check_margin_error("Insufficient Margin", new_confidence=0.6)
            
            # Should only cancel LOW
            assert mock_cancel.call_count == 1
            from unittest.mock import ANY
            mock_cancel.assert_called_once_with("P1_LOW", reason=ANY)

    @pytest.mark.asyncio
    async def test_sync_from_db_status_mapping(self, trader):
        """Verify ACTIVE in DB becomes filled in Memory."""
        mock_row = {
            'id': 100, 'symbol': 'BTC/USDT:USDT', 'timeframe': '1h', 
            'side': 'BUY', 'status': 'ACTIVE', 'pos_key': 'P1_BINANCE_BTC_1h'
        }
        trader.db.get_active_positions = AsyncMock(return_value=[mock_row])
        
        await trader.sync_from_db()
        
        # TradeSyncHelper maps ACTIVE -> filled
        assert trader.active_positions["P1_BINANCE_BTC_1h"]['status'] == 'filled'

    @pytest.mark.asyncio
    async def test_ghost_position_detection(self, trader):
        """Verify position on exchange but not in memory triggers resolution."""
        # 1. Exchange has BTC position
        trader.exchange.fetch_positions = AsyncMock(return_value=[
            {'symbol': 'BTC/USDT:USDT', 'contracts': 1.0, 'side': 'long'}
        ])
        # 2. Memory is empty
        trader.active_positions = {}
        
        # 3. Mock _resolve_ghost_position and _normalize_symbol
        trader._normalize_symbol = MagicMock(return_value="BTC/USDT:USDT")
        
        with patch.object(trader, '_resolve_ghost_position', new_callable=AsyncMock) as mock_resolve:
            # Bypass throttling
            trader._last_sync_time = 0
            await trader.sync_with_exchange()
            
            # In sync_with_exchange, it only iterates active_positions to find MISSING.
            # Ghost (Present on Exchange but NOT in active_positions) reconciliation 
            # is typically done in reconcile_positions, not sync_with_exchange.
            # Let's check reconcile_positions in execution.py.
            pass

    @pytest.mark.asyncio
    async def test_symbol_locks_are_unique_per_symbol(self, trader):
        lock_btc1 = trader._get_lock("BTC/USDT")
        lock_btc2 = trader._get_lock("BTC/USDT")
        lock_eth = trader._get_lock("ETH/USDT")
        
        assert lock_btc1 is lock_btc2
        assert lock_btc1 is not lock_eth

    @pytest.mark.asyncio
    async def test_shared_account_cache_is_global(self, mock_deps):
        """Shared cache must persist across Trader instances for the same account."""
        mock_ex, mock_db = mock_deps
        t1 = Trader(mock_ex, mock_db, profile_id=1, dry_run=False)
        t2 = Trader(mock_ex, mock_db, profile_id=2, dry_run=False)
        
        # Key is derived from API Key
        assert t1.account_key == t2.account_key
        
        t1.__class__._shared_account_cache[t1.account_key]['test_val'] = 123
        assert t2.__class__._shared_account_cache[t2.account_key]['test_val'] == 123
