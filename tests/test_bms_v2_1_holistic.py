import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from src.application.use_cases.execute_trade import ExecuteTradeUseCase
from src.application.trading.trade_orchestrator import TradeOrchestrator
from src.infrastructure.adapters.bybit_adapter import BybitAdapter
from src.domain.models import Trade


class TestBMSv21Holistic:
    @pytest.fixture
    def mock_repo(self):
        repo = MagicMock()
        repo.get_active_positions = AsyncMock(return_value=[])
        repo.get_active_positions_on_exchange = AsyncMock(return_value=[])
        repo.get_all_active_trade_profile_ids = AsyncMock(return_value=[])
        repo.get_profile_by_id = AsyncMock(return_value=None)
        repo.update_status = AsyncMock()
        repo.save_trade = AsyncMock()
        return repo

    @pytest.fixture
    def mock_adapter(self):
        adapter = MagicMock()
        adapter.close_position = AsyncMock()
        adapter.cancel_order = AsyncMock()
        adapter.set_position_sl_tp = AsyncMock()
        adapter.create_order = AsyncMock(return_value={'id': 'new_order_id'})
        adapter.fetch_ticker = AsyncMock(return_value={'last': 50000.0})
        adapter.fetch_balance = AsyncMock(return_value={'USDT': {'free': 100.0, 'total': 100.0}})
        adapter.check_min_notional = MagicMock(return_value=(True, "OK", 100.0))
        adapter.ensure_isolated_and_leverage = AsyncMock()
        adapter.can_trade = True
        return adapter

    @pytest.fixture
    def mock_sync_service(self):
        service = MagicMock()
        service.get_account_state = MagicMock(return_value={'positions': [], 'orders': []})
        service.profiles = [
            {'id': 1, 'name': 'Profile 1', 'exchange': 'BYBIT'},
            {'id': 2, 'name': 'Profile 2', 'exchange': 'BYBIT'}
        ]
        return service

    @pytest.fixture
    def execute_use_case(self, mock_repo, mock_adapter, mock_sync_service):
        risk = MagicMock()
        risk.calculate_sl_tp.return_value = (49000.0, 55000.0)
        risk.calculate_size_by_cost.return_value = 0.1 # Real number
        notif = MagicMock()
        notif.notify_generic = AsyncMock()
        notif.notify_order_filled = AsyncMock()
        notif.notify_order_pending = AsyncMock()
        cooldown = MagicMock()
        cooldown.is_in_cooldown = MagicMock(return_value=False)
        cooldown.is_margin_throttled = MagicMock(return_value=False)
        cooldown.handle_margin_error = AsyncMock()
        cooldown.set_sl_cooldown = AsyncMock()
        
        return ExecuteTradeUseCase(
            trade_repo=mock_repo,
            adapters={'BYBIT': mock_adapter},
            risk_service=risk,
            notification_service=notif,
            cooldown_manager=cooldown,
            sync_service=mock_sync_service
        )

    @pytest.mark.asyncio
    async def test_global_position_guard_blocks_duplicate(self, execute_use_case, mock_repo):
        """Verify that a trade is blocked if another profile already has an active trade on the same exchange."""
        profile = {'id': 2, 'exchange': 'BYBIT'}
        signal = {'symbol': 'BTC/USDT', 'side': 'BUY', 'confidence': 0.7, 'sl_pct': 0.02, 'tp_pct': 0.05}
        
        # Scenario: Another profile (1) already has an active BTC trade
        existing_trade = Trade(
            id=100, profile_id=1, symbol='BTC/USDT', side='BUY', 
            qty=0.1, entry_price=50000, status='ACTIVE', exchange='BYBIT'
        )
        mock_repo.get_active_positions.return_value = [existing_trade]
        
        result = await execute_use_case.execute(profile, signal)
        
        assert result is False
        execute_use_case.adapters['BYBIT'].create_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_signal_upgrade_replaces_pending(self, execute_use_case, mock_repo, mock_adapter):
        """Verify that a higher confidence signal replaces a PENDING order."""
        profile = {'id': 1, 'exchange': 'BYBIT'}
        new_signal = {'symbol': 'BTC/USDT', 'side': 'BUY', 'confidence': 0.8, 'sl_pct': 0.01, 'tp_pct': 0.1, 'support_level': 49500.0, 'Support': 49500.0}
        
        # Scenario: A low-confidence PENDING order exists
        pending_trade = Trade(
            id=100, profile_id=1, symbol='BTC/USDT', side='BUY', 
            qty=0.1, entry_price=50000, status='PENDING', exchange='BYBIT',
            exchange_order_id='old_oid',
            meta={'signal_confidence': 0.5, 'rr_ratio': 1.5}
        )
        mock_repo.get_active_positions.return_value = [pending_trade]
        
        result = await execute_use_case.execute(profile, new_signal)
        
        # Verify upgrade logic
        mock_adapter.cancel_order.assert_called_once_with('old_oid', 'BTC/USDT')
        mock_repo.update_status.assert_called_with(100, 'CANCELLED', exit_reason='UPGRADE')
        assert result is True # Should proceed to place new order

    @pytest.mark.asyncio
    async def test_position_optimization_updates_active_sl_tp(self, execute_use_case, mock_repo, mock_adapter, mock_sync_service):
        """Verify that a better signal for an ACTIVE trade updates SL/TP instead of opening new."""
        profile = {'id': 1, 'exchange': 'BYBIT'}
        better_signal = {'symbol': 'BTC/USDT', 'side': 'BUY', 'confidence': 0.9, 'sl_pct': 0.01, 'tp_pct': 0.08}
        
        # Scenario: An ACTIVE trade exists
        active_trade = Trade(
            id=101, profile_id=1, symbol='BTC/USDT', side='BUY', 
            qty=0.1, entry_price=50000, status='ACTIVE', exchange='BYBIT',
            meta={'signal_confidence': 0.6}
        )
        mock_repo.get_active_positions.return_value = [active_trade]
        
        # Need to ensure risk_service returns expected values
        execute_use_case.risk_service.calculate_sl_tp.return_value = (49500.0, 54000.0)
        
        # Mock exchange state: needs to HAVE the position for optimization to happen (Ghost Protection)
        mock_sync_service.get_account_state.return_value = {
            'positions': [{'symbol': 'BTCUSDT', 'side': 'BUY', 'entryPrice': 50000.0}],
            'orders': []
        }
        
        result = await execute_use_case.execute(profile, better_signal)
        
        assert result is False # We optimize, don't re-enter
        mock_adapter.set_position_sl_tp.assert_called_once_with('BTC/USDT', 'BUY', sl=49500.0, tp=54000.0)
        mock_repo.save_trade.assert_called_once()

    @pytest.mark.asyncio
    async def test_atomic_orchestration_deduplicates_signals(self, mock_sync_service):
        """Verify that TradeOrchestrator picks only one winner across profiles."""
        container = MagicMock()
        container.sync_service = mock_sync_service
        container.execute_trade_use_case.execute = AsyncMock()
        container.get_symbol_lock = MagicMock()
        
        # Mock evaluation: Both profiles see the same signals
        async def mock_eval(symbol, tf, exchange, profile_id=0):
            if tf == '4h': return {'symbol': 'BTC/USDT', 'side': 'BUY', 'confidence': 0.8}
            return {'symbol': 'BTC/USDT', 'side': 'SKIP', 'confidence': 0.1}
        container.evaluate_strategy_use_case.execute = AsyncMock(side_effect=mock_eval)
        
        orch = TradeOrchestrator(container)
        
        with patch('src.application.trading.trade_orchestrator.config') as mock_config:
            mock_config.BINANCE_SYMBOLS = []
            mock_config.BYBIT_SYMBOLS = ['BTC/USDT']
            mock_config.TRADING_TIMEFRAMES = ['1h', '4h']
            
            await orch._process_all_entries()
            
        # Verify execute was called EXACTLY ONCE globally
        assert container.execute_trade_use_case.execute.call_count == 1
        # It doesn't strictly matter which profile wins since they are identical in this test
        assert container.execute_trade_use_case.execute.called

    def test_bybit_adapter_symbol_normalization(self):
        """Verify that BybitAdapter correctly normalizes symbols for private API."""
        adapter = BybitAdapter(dry_run=True)
        assert adapter._get_bybit_symbol('BTC/USDT:USDT') == 'BTCUSDT'
        assert adapter._get_bybit_symbol('FLOW/USDT:USDT') == 'FLOWUSDT'
        assert adapter._get_bybit_symbol('BTCUSDT') == 'BTCUSDT'

    @pytest.mark.asyncio
    async def test_available_balance_guard_blocks_trade(self, execute_use_case, mock_repo, mock_adapter):
        """Verify that a trade is blocked if there's not enough available balance + 10% buffer."""
        profile = {'id': 1, 'exchange': 'BYBIT'}
        # high confidence -> cost_usdt = 8.0 -> required_margin = 8.8
        signal = {'symbol': 'BTC/USDT', 'side': 'BUY', 'confidence': 0.8, 'sl_pct': 0.02, 'tp_pct': 0.05}
        
        # Mock low balance by updating the return_value
        mock_adapter.fetch_balance.return_value = {'USDT': {'free': 5.0, 'total': 10.0}}
        
        result = await execute_use_case.execute(profile, signal)
        
        assert result is False
        mock_adapter.create_order.assert_not_called()
        # Cooldown handle margin error might be mocked differently across tests
        assert mock_adapter.fetch_balance.called or execute_use_case.cooldown_manager.handle_margin_error.called
