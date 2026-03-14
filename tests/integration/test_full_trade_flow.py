import pytest
import time
from unittest.mock import AsyncMock, MagicMock
from src.application.use_cases.execute_trade import ExecuteTradeUseCase
from src.application.use_cases.monitor_positions import MonitorPositionsUseCase
from src.infrastructure.repository.sqlite_trade_repository import SQLiteTradeRepository
from src.domain.services.risk_service import RiskService
from src.domain.services.notification_service import NotificationService
from src.cooldown_manager import CooldownManager
from src.application.trading.account_sync_service import AccountSyncService

@pytest.mark.asyncio
async def test_full_trade_flow_integration(mock_db):
    """
    Integration test: signal -> execute_trade -> save to DB -> monitor_positions -> detect fill -> ACTIVE
    """
    unique_name = f"Test Profile {int(time.time()*1000)}"
    p_id = await mock_db.add_profile(unique_name, "TEST", "BYBIT", api_key=f"TEST_KEY_{int(time.time()*1000)}")
    profile = {'id': p_id, 'exchange': 'BYBIT', 'api_key': f"TEST_KEY_{int(time.time()*1000)}"}

    # 2. Setup components
    trade_repo = SQLiteTradeRepository(mock_db)
    risk_service = RiskService()
    notification_service = AsyncMock(spec=NotificationService)
    cooldown_manager = CooldownManager(mock_db)

    # Mock adapter
    adapter = AsyncMock()
    adapter.can_trade = True
    adapter.account_key = f"BYBIT_TEST_KEY_{int(time.time()*1000)}"
    adapter.fetch_ticker.return_value = {'last': 0.0001}  # PEPE price
    adapter.fetch_balance.return_value = {'USDT': {'free': 1000.0}}
    adapter.fetch_positions.return_value = []
    adapter.fetch_open_orders.return_value = []
    adapter.price_to_precision = lambda s, p: str(round(p, 2))
    adapter.round_qty = lambda s, a: a
    adapter.ensure_isolated_and_leverage = AsyncMock()
    adapter.check_min_notional = MagicMock(return_value=(True, "", 100))
    adapter.create_order.return_value = {'id': f'order_{int(time.time()*1000)}', 'status': 'open'}

    adapters = {'BYBIT': adapter}

    # Sync service uses the real profile
    sync_service = AccountSyncService([profile], adapters)

    import logging
    import sys
    execute_trade = ExecuteTradeUseCase(
        trade_repo=trade_repo,
        adapters=adapters,
        risk_service=risk_service,
        notification_service=notification_service,
        cooldown_manager=cooldown_manager,
        sync_service=sync_service
    )

    execute_trade.logger.setLevel(logging.DEBUG)
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG)
    execute_trade.logger.addHandler(ch)
    
    monitor_positions = MonitorPositionsUseCase(
        sync_service=sync_service,
        trade_repo=trade_repo,
        risk_service=risk_service,
        notification_service=notification_service,
        cooldown_manager=cooldown_manager
    )

    unique_symbol = f"PEPE_{int(time.time()*1000)}/USDT:USDT"
    signal = {
        'symbol': unique_symbol,  # Unique symbol to avoid DB cross-contamination
        'side': 'BUY',
        'confidence': 0.8,
        'sl_pct': 0.02,
        'tp_pct': 0.04,
        'timeframe': '1h',
        'support_level': 0.000099  # Below ticker price 0.0001
    }

    success = await execute_trade.execute(profile, signal)
    assert success is True, "execute() should return True for a valid signal"

    # 4. Verify DB state: one PENDING trade
    trades = await trade_repo.get_active_positions(p_id)
    assert len(trades) == 1
    assert trades[0].status == 'PENDING'
    assert trades[0].symbol == unique_symbol

    # 5. Simulate Fill: order disappears from open orders, position appears
    adapter.fetch_open_orders.return_value = []
    adapter.fetch_positions.return_value = [{
        'symbol': unique_symbol,
        'contracts': 10000,
        'side': 'BUY',
        'entryPrice': 0.00009,
        'markPrice': 0.000095,
        'leverage': 10
    }]
    adapter.fetch_my_trades = AsyncMock(return_value=[{
        'symbol': unique_symbol,
        'id': 'fill_123',
        'order': trades[0].exchange_order_id,
        'side': 'buy',
        'price': 0.00009,
        'amount': 10000,
        'timestamp': int(time.time() * 1000)
    }])
    
    await sync_service.sync_all()
    await monitor_positions.execute()

    # 6. Verify status transitioned PENDING → ACTIVE
    trades = await trade_repo.get_active_positions(p_id)
    assert len(trades) == 1
    assert trades[0].status == 'ACTIVE'

    # 7. Verify notifications fired
    notification_service.notify_order_pending.assert_called()
    notification_service.notify_order_filled.assert_called()
