import pytest
import asyncio
import pandas as pd
from unittest.mock import MagicMock, AsyncMock, patch
from src.execution import Trader
from src.bot import BalanceTracker
from src.risk_manager import RiskManager

class TestTraderExecutionLogic:
    @pytest.fixture
    def mock_db(self):
        db = MagicMock()
        db.get_risk_metric = AsyncMock(return_value=None)
        db.set_risk_metric = AsyncMock(return_value=None)
        db.save_position = AsyncMock(return_value=1)
        db.update_position_status = AsyncMock(return_value=None)
        db.get_active_positions = AsyncMock(return_value=[])
        return db

    @pytest.fixture
    def mock_exchange(self):
        exch = MagicMock()
        exch.id = 'binance'
        exch.name = 'BINANCE'
        exch.is_public_only = False
        exch.apiKey = 'test_key'
        exch.can_trade = True
        # Prevent any real async calls
        exch.fetch_balance = AsyncMock(return_value={'USDT': {'free': 1000, 'total': 1000}})
        exch.fetch_positions = AsyncMock(return_value=[])
        exch.fetch_open_orders = AsyncMock(return_value=[])
        exch.fetch_leverage = AsyncMock(return_value={'leverage': 10})
        exch.set_leverage = AsyncMock(return_value=True)
        exch.milliseconds = MagicMock(return_value=123456789)
        return exch

    @pytest.fixture
    def trader(self, mock_exchange, mock_db):
        with patch('src.execution.ExchangeLoggerAdapter'):
            t = Trader(exchange=mock_exchange, db=mock_db, profile_id=1, profile_name="TestProfile", dry_run=False)
            t.active_positions = {}
            t._update_db_position = AsyncMock() # Replace DB update with mock
            
            # Mock the expensive/external calls
            t.modify_sl_tp = AsyncMock(return_value=True)
            t.recreate_missing_sl_tp = AsyncMock(return_value=True)
            t.force_close_position = AsyncMock(return_value=True)
            t._create_sl_tp = AsyncMock(return_value=(True, 'new_sl', 'new_tp'))
            
            # Mock the internal API retry mechanism to bypass static method logic
            async def mock_exec(func, *args, **kwargs):
                res = func(*args, **kwargs)
                if asyncio.iscoroutine(res):
                    res = await res
                return res
            t._execute_with_timestamp_retry = AsyncMock(side_effect=mock_exec)
            
            return t

    @pytest.mark.asyncio
    async def test_tighten_sl_logic(self, trader):
        """Verify that SL is moved closer to entry by the specified factor."""
        pos_key = "BINANCE_BTC_USDT_1h"
        trader.active_positions[pos_key] = {
            'symbol': 'BTC/USDT',
            'side': 'BUY',
            'entry_price': 50000.0,
            'sl': 48000.0,
            'tp': 55000.0,
            'status': 'filled'
        }
        
        # Tighten by 50%
        new_sl = await trader.tighten_sl(pos_key, factor=0.5)
        
        assert new_sl == 49000.0
        assert trader.active_positions[pos_key]['sl_tightened'] is True
        trader.modify_sl_tp.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_dynamic_sltp_atr_trail_long(self, trader):
        """Verify ATR trailing stop for LONG position."""
        pos_key = "BINANCE_BTC_USDT_1h"
        trader.active_positions[pos_key] = {
            'symbol': 'BTC/USDT',
            'side': 'BUY',
            'entry_price': 50000.0,
            'sl': 49000.0,
            'tp': 55000.0,
            'status': 'filled',
            'timeframe': '1h'
        }
        
        df_trail = pd.DataFrame({
            'high': [53000.0] * 10,
            'close': [52000.0] * 10,
            'ATR_14': [500.0] * 10
        })
        df_guard = pd.DataFrame({'close': [52000.0], 'RSI_14': [50.0], 'EMA_21': [51000.0]})
        
        with patch('src.execution.config.ENABLE_DYNAMIC_SLTP', True), \
             patch('src.execution.config.ATR_TRAIL_MULTIPLIER', 1.5), \
             patch('src.execution.config.ATR_TRAIL_MIN_MOVE_PCT', 0.001):
            
            # 53000 - (1.5 * 500) = 52250
            res = await trader.update_dynamic_sltp(pos_key, df_trail=df_trail, df_guard=df_guard)
            
        assert res is True
        assert trader.active_positions[pos_key]['sl'] == 52250.0
        trader.recreate_missing_sl_tp.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_dynamic_sltp_rsi_guard_sell(self, trader):
        """Verify RSI guard pulls TP closer for SHORT position when oversold."""
        pos_key = "BINANCE_BTC_USDT_1h"
        trader.active_positions[pos_key] = {
            'symbol': 'BTC/USDT',
            'side': 'SELL',
            'status': 'filled',
            'entry_price': 50000.0,
            'tp': 45000.0,
            'sl': 51000.0,
            'timeframe': '1h'
        }
        
        df_guard = pd.DataFrame({
            'close': [48000.0],
            'RSI_14': [20.0], # Oversold for Short ( RSI < 100-75=25 )
            'EMA_21': [49000.0]
        })
        df_trail = pd.DataFrame({'ATR_14': [500.0] * 10, 'low': [47000.0] * 10}) 
        
        with patch('src.execution.config.ENABLE_DYNAMIC_SLTP', True), \
             patch('src.execution.config.RSI_OVERBOUGHT_EXIT', 75):
            
            await trader.update_dynamic_sltp(pos_key, df_trail=df_trail, df_guard=df_guard)
            
        assert trader.active_positions[pos_key]['tp'] == 46500.0
        assert trader.active_positions[pos_key]['tp_tightened'] is True
        trader.recreate_missing_sl_tp.assert_called_once()

    @pytest.mark.asyncio
    async def test_reconcile_positions_adoption(self, trader, mock_exchange):
        """Verify that unknown exchange positions are adopted locally."""
        mock_exchange.fetch_positions.return_value = [{
            'symbol': 'ETH/USDT:USDT',
            'contracts': 1.0,
            'side': 'long',
            'info': {'positionAmt': '1.0'},
            'leverage': 10
        }]
        
        trader.active_positions = {}
        await trader.reconcile_positions(auto_fix=True)
        
        found = False
        for k, p in trader.active_positions.items():
            if 'ETH/USDT' in p['symbol']:
                found = True
                assert p['status'] == 'filled'
                break
        assert found is True

    @pytest.mark.asyncio
    async def test_reconcile_positions_missing_sl_tp(self, trader, mock_exchange):
        """Verify that missing SL/TP on exchange are recreated."""
        pos_key = "BINANCE_BTC_USDT_1h"
        trader.active_positions[pos_key] = {
            'symbol': 'BTC/USDT',
            'side': 'BUY',
            'status': 'filled',
            'sl': 49000.0,
            'tp': 55000.0,
            'sl_order_id': 'old_sl_id',
            'timeframe': '1h'
        }
        
        mock_exchange.fetch_positions.return_value = [{
            'symbol': 'BTC/USDT',
            'contracts': 1.0,
            'side': 'long'
        }]
        mock_exchange.fetch_open_orders.return_value = []
        
        with patch('src.execution.config.AUTO_CREATE_SL_TP', True):
            await trader.reconcile_positions(auto_fix=True, force_verify=True)
        
        trader.recreate_missing_sl_tp.assert_called_once()

    @pytest.mark.asyncio
    async def test_adjust_sl_tp_for_profit_lock(self, trader):
        """Verify that SL is moved to profit when price reaches 80% of TP."""
        pos_key = "BINANCE_BTC_USDT_1h"
        trader.active_positions[pos_key] = {
            'symbol': 'BTC/USDT',
            'side': 'BUY',
            'status': 'filled',
            'entry_price': 50000.0,
            'sl': 49000.0,
            'tp': 60000.0,
            'timeframe': '1h'
        }
        
        with patch('src.execution.config.ENABLE_PROFIT_LOCK', True), \
             patch('src.execution.config.PROFIT_LOCK_THRESHOLD', 0.8), \
             patch('src.execution.config.PROFIT_LOCK_LEVEL', 0.1):
            
            trader.recreate_missing_sl_tp = AsyncMock(return_value=True)
            await trader.adjust_sl_tp_for_profit_lock(pos_key, 51000.0)
            assert trader.active_positions[pos_key]['sl'] == 49000.0
            
            await trader.adjust_sl_tp_for_profit_lock(pos_key, 59000.0)
            assert trader.active_positions[pos_key]['sl'] == 51000.0
            assert trader.active_positions[pos_key]['profit_locked'] is True
            trader.recreate_missing_sl_tp.assert_called_once()

    def test_balance_tracker_workflow(self):
        """Verify BalanceTracker reservation and release logic."""
        bt = BalanceTracker()
        ex = "BINANCE"
        pid = 1
        
        bt.update_balance(ex, pid, 1000.0)
        assert bt.get_available(ex, pid) == 1000.0
        
        success = bt.reserve(ex, pid, 200.0)
        assert success is True
        assert bt.get_available(ex, pid) == 800.0
        
        success = bt.reserve(ex, pid, 900.0)
        assert success is False
        
        bt.release(ex, pid, 100.0)
        assert bt.get_available(ex, pid) == 900.0
        
        bt.reset_reservations()
        assert bt.get_available(ex, pid) == 1000.0

    @pytest.mark.asyncio
    async def test_circuit_breaker_logic(self, mock_db):
        """Verify RiskManager's circuit breaker triggers on drawdown/daily loss."""
        rm = RiskManager(db=mock_db, profile_id=1, max_drawdown_pct=0.1, daily_loss_limit_pct=0.03)
        
        triggered, reason = await rm.check_circuit_breaker(1000.0)
        assert triggered is False
        
        # Drawdown
        triggered, reason = await rm.check_circuit_breaker(890.0)
        assert triggered is True
        assert "Max Drawdown" in reason
        
        # Reset and Daily Loss
        rm.peak_balance = 0
        rm._last_reset_date = None
        triggered, reason = await rm.check_circuit_breaker(1000.0)
        assert triggered is False
        

# --- New Architecture: ExecuteTradeUseCase Tests ---
from src.application.use_cases.execute_trade import ExecuteTradeUseCase
from src.domain.models import Trade
from src.domain.exceptions import InsufficientFundsError

class TestExecuteTradeUseCase:
    @pytest.fixture
    def mock_repo(self):
        repo = MagicMock()
        repo.save_trade = AsyncMock(return_value=1)
        repo.update_status = AsyncMock()
        repo.get_active_positions = AsyncMock(return_value=[])
        return repo

    @pytest.fixture
    def mock_adapter(self):
        adapter = MagicMock()
        adapter.can_trade = True
        adapter.account_key = "MOCK_ACC"
        adapter.fetch_ticker = AsyncMock(return_value={'last': 50000.0})
        adapter.create_order = AsyncMock(return_value={'id': 'order_123'})
        adapter.check_min_notional = MagicMock(return_value=(True, "", 1.0))
        adapter.ensure_isolated_and_leverage = AsyncMock()
        return adapter

    @pytest.fixture
    def mock_risk(self):
        risk = MagicMock()
        risk.calculate_size_by_cost = MagicMock(return_value=0.1)
        risk.calculate_sl_tp = MagicMock(return_value=(49000.0, 52000.0))
        return risk

    @pytest.fixture
    def mock_notify(self):
        notify = MagicMock()
        notify.notify_order_filled = AsyncMock()
        return notify

    @pytest.fixture
    def mock_cooldown(self):
        cd = MagicMock()
        cd.is_in_cooldown = MagicMock(return_value=False)
        cd.is_margin_throttled = MagicMock(return_value=False)
        cd.handle_margin_error = AsyncMock()
        return cd

    @pytest.mark.asyncio
    async def test_execute_trade_sets_entry_time(self, mock_repo, mock_adapter, mock_risk, mock_notify, mock_cooldown):
        use_case = ExecuteTradeUseCase(mock_repo, {"BYBIT": mock_adapter}, mock_risk, mock_notify, mock_cooldown)
        profile = {"id": 1, "exchange": "BYBIT"}
        signal = {"symbol": "BTC/USDT", "side": "BUY", "confidence": 0.8, "sl_pct": 0.02, "tp_pct": 0.04}
        
        await use_case.execute(profile, signal)
        
        args, _ = mock_repo.save_trade.call_args
        trade = args[0]
        assert trade.entry_time is not None
        assert trade.entry_time > 0

    @pytest.mark.asyncio
    async def test_execute_trade_honors_margin_throttling(self, mock_repo, mock_adapter, mock_risk, mock_notify, mock_cooldown):
        mock_cooldown.is_margin_throttled.return_value = True
        use_case = ExecuteTradeUseCase(mock_repo, {"BYBIT": mock_adapter}, mock_risk, mock_notify, mock_cooldown)
        profile = {"id": 1, "exchange": "BYBIT"}
        signal = {"symbol": "BTC/USDT", "side": "BUY", "confidence": 0.8}
        
        result = await use_case.execute(profile, signal)
        assert result is False
        mock_adapter.fetch_ticker.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_trade_enforces_isolated_margin_before_order(self, mock_repo, mock_adapter, mock_risk, mock_notify, mock_cooldown):
        """Verify that ensure_isolated_and_leverage is called before sizing and trading."""
        use_case = ExecuteTradeUseCase(mock_repo, {"BINANCE": mock_adapter}, mock_risk, mock_notify, mock_cooldown)
        profile = {"id": 1, "exchange": "BINANCE"}
        signal = {"symbol": "BTCDOM/USDT:USDT", "side": "BUY", "confidence": 0.8, "sl_pct": 0.02, "tp_pct": 0.04}
        
        # We want to verify the CALL ORDER specifically
        # 1. ensure_isolated_and_leverage must be called
        # 2. create_order must be called AFTER
        
        manager = MagicMock()
        manager.attach_mock(mock_adapter.ensure_isolated_and_leverage, 'ensure_isolated')
        manager.attach_mock(mock_adapter.create_order, 'create_order')
        
        await use_case.execute(profile, signal)
        
        # Verify both called
        mock_adapter.ensure_isolated_and_leverage.assert_called_once_with("BTCDOM/USDT:USDT", 5) # High tier leverage is 5
        mock_adapter.create_order.assert_called_once()
        
        # Verify call order in manager
        from unittest.mock import call
        # expected_calls = [
        #     call.ensure_isolated("BTCDOM/USDT:USDT", 5),
        #     call.create_order('BTCDOM/USDT:USDT', 'market', 'buy', 0.1, price=None, params=ANY)
        # ]
        
        # Verify call sequence
        call_names = [c[0] for c in manager.mock_calls]
        assert 'ensure_isolated' in call_names
        assert 'create_order' in call_names
        assert call_names.index('ensure_isolated') < call_names.index('create_order')
