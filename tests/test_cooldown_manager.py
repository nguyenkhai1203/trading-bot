import pytest
import time
import json
from unittest.mock import MagicMock, AsyncMock, patch
from src.cooldown_manager import CooldownManager

class TestCooldownManager:
    """
    Test suite for CooldownManager.
    Covers: SL cooldowns, Margin throttling, and DB persistence.
    """

    @pytest.fixture
    def mock_db(self):
        return AsyncMock()

    @pytest.fixture
    def manager(self, mock_db):
        return CooldownManager(mock_db, MagicMock())

    @pytest.mark.asyncio
    async def test_sl_cooldown_logic(self, manager):
        """Verify symbol-level cooldown after SL."""
        exchange = "BINANCE"
        symbol = "BTC/USDT"
        profile_id = 1
        
        # 1. Initially no cooldown
        assert manager.is_in_cooldown(exchange, symbol) is False
        
        # 2. Set cooldown for 1 hour
        await manager.set_sl_cooldown(exchange, symbol, profile_id, custom_duration=3600)
        assert manager.is_in_cooldown(exchange, symbol) is True
        assert manager.get_remaining_minutes(exchange, symbol) > 59
        
        # 3. Verify expiry
        with patch("src.cooldown_manager.time.time", return_value=time.time() + 4000):
            assert manager.is_in_cooldown(exchange, symbol) is False

    @pytest.mark.asyncio
    async def test_margin_throttling_logic(self, manager):
        """Verify account-level margin throttling via shared cache."""
        account_key = "MOCK_ACC_1"
        shared_cache = {account_key: {}}
        
        # 1. Initially not throttled
        assert manager.is_margin_throttled(account_key, shared_cache) is False
        
        # 2. Handle margin error
        await manager.handle_margin_error(account_key, shared_cache, "BINANCE")
        
        # 3. Should now be throttled
        assert manager.is_margin_throttled(account_key, shared_cache) is True
        assert shared_cache[account_key]['margin_cooldown_until'] > time.time()

    @pytest.mark.asyncio
    async def test_db_sync_hydration(self, manager):
        """Verify cooldowns are hydrated from DB JSON."""
        profile_id = 1
        future_time = time.time() + 1000
        mock_json = json.dumps({"BINANCE:BTC/USDT": future_time})
        manager.db.get_risk_metric = AsyncMock(return_value=mock_json)
        
        await manager.sync_from_db(profile_id)
        
        assert manager.is_in_cooldown("BINANCE", "BTC/USDT") is True
        assert "BINANCE:BTC/USDT" in manager._sl_cooldowns

    @pytest.mark.asyncio
    async def test_save_to_db_filters_expired(self, manager):
        """Verify expired cooldowns are not saved to DB."""
        now = time.time()
        manager._sl_cooldowns = {
            "BINANCE:BTC": now + 1000,
            "BINANCE:ETH": now - 1000
        }
        
        await manager.save_to_db(profile_id=1)
        
        # Check call arguments
        call_args = manager.db.set_risk_metric.call_args
        saved_data = json.loads(call_args[0][2])
        assert "BINANCE:BTC" in saved_data
        assert "BINANCE:ETH" not in saved_data
