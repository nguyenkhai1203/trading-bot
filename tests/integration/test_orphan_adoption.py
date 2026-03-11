import pytest
from unittest.mock import AsyncMock
from src.application.use_cases.monitor_positions import MonitorPositionsUseCase
from src.infrastructure.repository.sqlite_trade_repository import SQLiteTradeRepository
from src.domain.services.risk_service import RiskService
from src.domain.services.notification_service import NotificationService
from src.cooldown_manager import CooldownManager
from src.application.trading.account_sync_service import AccountSyncService

@pytest.mark.asyncio
async def test_orphan_adoption_integration(mock_db):
    """
    Integration test: Position exists on exchange but NOT in DB -> must be adopted
    """
    # 1. Seed Profile FIRST for a real auto-incremented ID
    p_id = await mock_db.add_profile("Test Profile 2", "TEST", "BYBIT", api_key="KEY2")
    profile = {'id': p_id, 'exchange': 'BYBIT', 'api_key': 'KEY2'}

    trade_repo = SQLiteTradeRepository(mock_db)
    risk_service = RiskService()
    notification_service = AsyncMock(spec=NotificationService)
    cooldown_manager = CooldownManager(mock_db)

    # Mock adapter: one open position, no orders in DB
    adapter = AsyncMock()
    adapter.fetch_positions.return_value = [{
        'symbol': 'ETH/USDT:USDT',
        'contracts': 1.0,
        'side': 'SELL',
        'entryPrice': 2500,
        'markPrice': 2450,
        'leverage': 5,
        'stopLoss': 2600,
        'takeProfit': 2200
    }]
    adapter.fetch_balance.return_value = {}
    adapter.fetch_open_orders.return_value = []

    adapters = {'BYBIT': adapter}
    sync_service = AccountSyncService([profile], adapters)

    monitor_positions = MonitorPositionsUseCase(
        sync_service=sync_service,
        trade_repo=trade_repo,
        risk_service=risk_service,
        notification_service=notification_service,
        cooldown_manager=cooldown_manager
    )

    # 2. Sync exchange state into cache
    await sync_service.sync_all()

    # 3. Run the monitor — should detect ETH/USDT:USDT as an orphan and adopt it
    await monitor_positions.execute()

    # 4. Verify: orphan must now be in DB as ACTIVE
    trades = await trade_repo.get_active_positions(p_id)
    assert len(trades) == 1, f"Expected 1 adopted trade, got {len(trades)}"
    trade = trades[0]
    assert trade.symbol == 'ETH/USDT:USDT'
    assert trade.side == 'SELL'
    assert trade.qty == 1.0
    assert trade.meta.get('is_orphan') is True
    # notify_generic is a best-effort side-effect verified in unit tests; omitted here.
