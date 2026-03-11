import time
import pytest
from unittest.mock import AsyncMock
from src.application.use_cases.monitor_positions import MonitorPositionsUseCase
from src.infrastructure.repository.sqlite_trade_repository import SQLiteTradeRepository
from src.domain.services.risk_service import RiskService
from src.domain.services.notification_service import NotificationService
from src.cooldown_manager import CooldownManager
from src.application.trading.account_sync_service import AccountSyncService
from src.domain.models import Trade

@pytest.mark.asyncio
async def test_ghost_pending_cleanup_integration(mock_db):
    """
    Integration test: PENDING trade in DB but missing from exchange orders AND positions -> mark CANCELLED
    """
    # 1. Seed profile first to get a real auto-incremented ID (isolated from other tests)
    p_id = await mock_db.add_profile("Ghost Test Profile", "TEST", "BYBIT", api_key=f"GHOST_KEY_{int(time.time())}")
    profile = {'id': p_id, 'exchange': 'BYBIT', 'api_key': f"GHOST_KEY_{int(time.time())}"}

    trade_repo = SQLiteTradeRepository(mock_db)
    risk_service = RiskService()
    notification_service = AsyncMock(spec=NotificationService)
    cooldown_manager = CooldownManager(mock_db)

    # 2. Add a PENDING trade from 10 minutes ago (past grace period)
    # Using a fully unique symbol and pos_key to prevent any cross-test DB persistence collisions.
    unique_symbol = f"SOL_GHOST_{int(time.time() * 1000)}/USDT:USDT"
    old_trade = Trade(
        profile_id=p_id,
        exchange='BYBIT',
        symbol=unique_symbol,
        side='BUY',
        qty=10,
        entry_price=100,
        status='PENDING',
        timeframe='1h',
        pos_key=f"BYBIT_{unique_symbol.replace('/','_')}_{p_id}",
        entry_time=int(time.time() * 1000) - 600_000,  # 10 min ago
        exchange_order_id=f"missing_oid_{int(time.time() * 1000)}"
    )
    await trade_repo.save_trade(old_trade)

    # 3. Mock adapter: order is missing from exchange (ghost scenario)
    adapter = AsyncMock()
    adapter.fetch_balance.return_value = {}
    adapter.fetch_positions.return_value = []
    adapter.fetch_open_orders.return_value = []  # The order is gone!

    adapters = {'BYBIT': adapter}
    sync_service = AccountSyncService([profile], adapters)

    monitor_positions = MonitorPositionsUseCase(
        sync_service=sync_service,
        trade_repo=trade_repo,
        risk_service=risk_service,
        notification_service=notification_service,
        cooldown_manager=cooldown_manager
    )

    # 4. Sync & Run monitor
    await sync_service.sync_all()
    await monitor_positions.execute()

    # 5. Verify: PENDING trade is now CANCELLED (no longer in active positions)
    active = await trade_repo.get_active_positions(p_id)
    assert len(active) == 0, f"Expected 0 active trades, got {len(active)}: {[t.symbol for t in active]}"

    # 6. Verify notification was sent (using assert_called for emoji-safe comparison)
    notification_service.notify_generic.assert_called()
