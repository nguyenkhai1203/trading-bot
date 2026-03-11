import pytest
import time
from unittest.mock import AsyncMock, MagicMock
from src.application.use_cases.execute_trade import ExecuteTradeUseCase
from src.infrastructure.repository.sqlite_trade_repository import SQLiteTradeRepository
from src.domain.services.risk_service import RiskService
from src.domain.services.notification_service import NotificationService
from src.cooldown_manager import CooldownManager

@pytest.mark.asyncio
async def test_reentry_cooldown_integration(mock_db):
    """
    Integration test: After SL detected, cooldown must block next signal for same symbol+profile
    """
    trade_repo = SQLiteTradeRepository(mock_db)
    risk_service = RiskService()
    notification_service = AsyncMock(spec=NotificationService)
    cooldown_manager = CooldownManager(mock_db)
    
    adapter = AsyncMock()
    adapter.can_trade = True
    adapter.account_key = "BYBIT_TEST_KEY"
    adapters = {'BYBIT': adapter}
    
    execute_trade = ExecuteTradeUseCase(
        trade_repo=trade_repo,
        adapters=adapters,
        risk_service=risk_service,
        notification_service=notification_service,
        cooldown_manager=cooldown_manager
    )
    
    profile = {'id': 1, 'exchange': 'BYBIT', 'api_key': 'TEST_KEY'}
    symbol = 'BTC/USDT:USDT'
    
    # 1. Manually set cooldown
    # Key format: BYBIT:1:BTC/USDT:USDT
    await cooldown_manager.set_sl_cooldown('BYBIT', symbol, 1, custom_duration=3600)
    
    # 2. Try to execute trade
    signal = {
        'symbol': symbol,
        'side': 'BUY',
        'confidence': 0.8,
        'sl_pct': 0.02,
        'tp_pct': 0.04,
        'timeframe': '1h'
    }
    
    success = await execute_trade.execute(profile, signal)
    
    # 3. Assert blocked
    assert success is False
    # Check that it didn't even reach fetch_ticker
    adapter.fetch_ticker.assert_not_called()
