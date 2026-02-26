import pytest
import asyncio
import pandas as pd
from unittest.mock import MagicMock, AsyncMock, patch
from execution import Trader
from bot import BalanceTracker
from risk_manager import RiskManager

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
        with patch('execution.ExchangeLoggerAdapter'):
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
        
        with patch('execution.ENABLE_DYNAMIC_SLTP', True), \
             patch('execution.ATR_TRAIL_MULTIPLIER', 1.5), \
             patch('execution.ATR_TRAIL_MIN_MOVE_PCT', 0.001):
            
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
        
        with patch('execution.ENABLE_DYNAMIC_SLTP', True), \
             patch('execution.RSI_OVERBOUGHT_EXIT', 75):
            
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
        
        with patch('execution.AUTO_CREATE_SL_TP', True):
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
        
        with patch('execution.ENABLE_PROFIT_LOCK', True), \
             patch('execution.PROFIT_LOCK_THRESHOLD', 0.8), \
             patch('execution.PROFIT_LOCK_LEVEL', 0.1):
            
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
        
        triggered, reason = await rm.check_circuit_breaker(960.0)
        assert triggered is True
        assert "Daily Loss" in reason
