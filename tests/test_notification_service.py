import pytest
import time
import asyncio
from datetime import datetime
from unittest.mock import MagicMock, AsyncMock, patch
from src.domain.services.notification_service import NotificationService
from src.domain.models import Trade

class TestNotificationService:
    @pytest.fixture
    def mock_repo(self):
        return MagicMock()

    @pytest.fixture
    def mock_adapter(self):
        return MagicMock()

    @pytest.fixture
    def service(self):
        return NotificationService()

    @pytest.mark.asyncio
    async def test_notify_position_closed_graceful_timestamps(self, service):
        """Verify that position closed notification handles missing/null timestamps."""
        trade = Trade(
            profile_id=1,
            exchange="BYBIT",
            symbol="BTC/USDT",
            side="BUY",
            qty=1.0,
            entry_price=50000.0,
            status="CLOSED",
            timeframe="1h",
            pos_key="MOCK_KEY",
            exit_price=51000.0,
            exit_reason="TP",
            entry_time=None, # Missing entry_time
            exit_time=None   # Missing exit_time
        )
        
        with patch('src.infrastructure.notifications.notification.format_position_closed') as mock_format, \
             patch('src.infrastructure.notifications.notification.send_telegram_message', new_callable=AsyncMock) as mock_send:
            
            mock_format.return_value = ("terminal msg", "telegram msg")
            
            await service.notify_position_closed(trade, exit_price=51000.0, pnl=100.0, pnl_pct=2.0, reason="TP")
            
            # Verify that it didn't crash.
            mock_format.assert_called_once()
            mock_send.assert_called_once()

    @pytest.mark.asyncio
    async def test_notify_order_filled(self, service):
        """Verify order fill notification sends correctly."""
        trade = Trade(
            profile_id=1,
            exchange="BYBIT",
            symbol="BTC/USDT",
            side="BUY",
            qty=1.0,
            entry_price=50000.0,
            status="ACTIVE",
            timeframe="1h",
            pos_key="MOCK_KEY"
        )
        
        with patch('src.infrastructure.notifications.notification.send_telegram_message', new_callable=AsyncMock) as mock_send:
            await service.notify_order_filled(trade, score=0.8)
            mock_send.assert_called_once()
            args, _ = mock_send.call_args
            assert "FILLED" in args[0]
            assert "BTC/USDT" in args[0]
            assert "BUY" in args[0]
