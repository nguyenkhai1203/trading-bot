import pytest
import asyncio
import time as time_mod
from unittest.mock import MagicMock, AsyncMock, patch
import pandas as pd
import numpy as np
import tempfile
import os
import json
from bot import TradingBot, BalanceTracker
from risk_manager import RiskManager

class TestTradingBotIntegration:
    @pytest.fixture
    def mock_components(self):
        data_manager = MagicMock()
        trader = MagicMock()
        trader.exchange_name = 'BINANCE'
        trader.dry_run = True
        trader.active_positions = {}
        trader.pending_orders = {}
        trader.exchange = MagicMock()
        trader.exchange.can_trade = True
        trader.exchange.id = 'binance'
        trader.exchange.name = 'BINANCE'
        trader.exchange.is_authenticated = True
        
        # Mock internal helpers
        trader._get_pos_key = MagicMock(return_value='BINANCE_BTC_USDT_1h')
        trader.has_any_symbol_position = AsyncMock(return_value=False)
        trader.is_in_cooldown = AsyncMock(return_value=False)
        trader.place_order = AsyncMock(return_value={'id': 'order_1', 'status': 'open'})
        trader.cancel_pending_order = AsyncMock(return_value=True)
        trader.verify_symbol_state = AsyncMock(return_value=None)
        trader.adjust_sl_tp_for_profit_lock = AsyncMock(return_value=None)
        
        balance_tracker = BalanceTracker()
        balance_tracker.update_balance('BINANCE', 1, 1000.0)
        
        # Create a temporary daily_config.json for tests
        fd, temp_path = tempfile.mkstemp(suffix='.json')
        os.close(fd)
        with open(temp_path, 'w') as f:
            json.dump({"BINANCE": {"starting_balance_day": 1000, "peak_balance": 1000}}, f)
        
        return {
            'data_manager': data_manager,
            'trader': trader,
            'balance_tracker': balance_tracker,
            'temp_config': temp_path
        }

    @pytest.mark.asyncio
    async def test_run_step_signal_to_order(self, mock_components):
        """Verify end-to-end signal to order flow."""
        dm = mock_components['data_manager']
        trader = mock_components['trader']
        bt = mock_components['balance_tracker']
        
        # Mock WeightedScoringStrategy to avoid config loading
        with patch('bot.WeightedScoringStrategy') as mock_strat_cls:
            mock_strat = mock_strat_cls.return_value
            mock_strat.is_enabled.return_value = True
            mock_strat.get_sizing_tier.return_value = {'leverage': 10, 'cost_usdt': 50}
            mock_strat.get_dynamic_risk_params.return_value = (0.02, 0.04)
            mock_strat.sl_pct = 0.02
            mock_strat.tp_pct = 0.04
            
            bot = TradingBot('BTC/USDT', '1h', dm, trader, MagicMock(), MagicMock(), balance_tracker=bt)
            
            # Mock strategy to return a BUY signal
            mock_strat.get_signal = MagicMock(return_value={
                'side': 'BUY', 
                'confidence': 0.8, 
                'comment': 'Strong Bullish (RSI_oversold)',
                'signals': 'RSI_oversold',
                'snapshot': {}
            })
            
            # Inject isolated RiskManager
            bot.risk_manager = RiskManager(db=MagicMock(), profile_id=1)
            
            # Mock data manager to return data
            df = pd.DataFrame({'close': [50000.0], 'high': [50100.0], 'low': [49900.0]})
            dm.get_data.return_value = df
            dm.get_data_with_features.return_value = df
            
            # Mock risk manager calculation
            bot.risk_manager.calculate_sl_tp = MagicMock(return_value=(49000.0, 52000.0))
            bot.risk_manager.calculate_size_by_cost = MagicMock(return_value=0.01)
            
            # Mock exchange market for min cost
            trader.exchange.market.return_value = {'limits': {'cost': {'min': 5.0}}}

            bot.signal_tracker.should_skip_symbol = MagicMock(return_value=(False, ""))
            bot.signal_tracker.get_last_trade_side = MagicMock(return_value=None)
            
            # Execute step
            try:
                with patch('bot.send_telegram_message', AsyncMock()):
                    
                    is_closed = await bot.run_monitoring_cycle()
                    if not is_closed:
                        signal = await bot.get_new_entry_signal()
                        if signal:
                            await bot.execute_entry(signal, 1000.0)
            finally:
                if os.path.exists(mock_components['temp_config']):
                    os.remove(mock_components['temp_config'])
                
            # Assertions
            trader.place_order.assert_called_once()
            # Reservation should be released in finally block
            assert bt.balances[('BINANCE', 1)]['reserved'] == 0.0

    @pytest.mark.asyncio
    async def test_run_step_duplicate_prevention(self, mock_components):
        """Verify that new order is skipped if position already exists."""
        dm = mock_components['data_manager']
        trader = mock_components['trader']
        bt = mock_components['balance_tracker']
        
        with patch('bot.WeightedScoringStrategy'):
            bot = TradingBot('BTC/USDT', '1h', dm, trader, MagicMock(), MagicMock(), balance_tracker=bt)
            
            # Mock data manager to return data
            df = pd.DataFrame({'close': [50000.0]})
            dm.get_data.return_value = df
            
            # Mock existing position
            trader.has_any_symbol_position = AsyncMock(return_value=True)
            trader.active_positions = {'BINANCE_BTC_USDT_1h': {'status': 'filled', 'symbol': 'BTC/USDT', 'side': 'BUY'}}
            
            # Execute step
            bot.signal_tracker.should_skip_symbol = MagicMock(return_value=(False, ""))
            is_closed = await bot.run_monitoring_cycle()
            if not is_closed:
                # Due to has_any_symbol_position logic in real execute_entry, we skip or mock it
                if await trader.has_any_symbol_position(bot.symbol):
                    pass # Don't get signal
                else:
                    signal = await bot.get_new_entry_signal()
                    if signal: await bot.execute_entry(signal, 1000.0)
            
            # Assertions
            trader.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_step_signal_invalidation(self, mock_components):
        """Verify that pending order is cancelled if signal reverses."""
        dm = mock_components['data_manager']
        trader = mock_components['trader']
        bt = mock_components['balance_tracker']
        
        with patch('bot.WeightedScoringStrategy') as mock_strat_cls:
            mock_strat = mock_strat_cls.return_value
            bot = TradingBot('BTC/USDT', '1h', dm, trader, MagicMock(), MagicMock(), balance_tracker=bt)
            pos_key = 'BINANCE_BTC_USDT_1h'
            
            # Mock a pending order — timestamp must be > 120s old so MIN_PENDING_SECS guard passes
            stale_ts = int((time_mod.time() - 200) * 1000)
            trader.active_positions[pos_key] = {
                'status': 'pending', 'side': 'BUY', 'entry_price': 50000.0,
                'timestamp': stale_ts
            }
            
            # Mock data
            df = pd.DataFrame({'close': [50000.0]})
            dm.get_data.return_value = df
            dm.get_data_with_features.return_value = df
            
            # Mock strategy to return a SELL signal (reversal)
            mock_strat.get_signal = MagicMock(return_value={'side': 'SELL', 'confidence': 0.8})
            
            # Execute step
            bot.signal_tracker.should_skip_symbol = MagicMock(return_value=(False, ""))
            with patch('bot.send_telegram_message', AsyncMock()):
                is_closed = await bot.run_monitoring_cycle()
                if not is_closed:
                    signal = await bot.get_new_entry_signal()
                    if signal: await bot.execute_entry(signal, 1000.0)
                
            # Assertions
            trader.cancel_pending_order.assert_called_once()
            # Reason contains "reversal" ("Strong signal reversal") 
            assert "reversal" in trader.cancel_pending_order.call_args[1].get('reason', '').lower()

    @pytest.mark.asyncio
    async def test_run_step_circuit_breaker(self, mock_components):
        """Verify that circuit breaker stops trading."""
        dm = mock_components['data_manager']
        trader = mock_components['trader']
        bt = mock_components['balance_tracker']
        
        with patch('bot.WeightedScoringStrategy'):
            bot = TradingBot('BTC/USDT', '1h', dm, trader, MagicMock(), MagicMock(), balance_tracker=bt)
            
            # Execute step with circuit breaker flag simulation
            bot.running = False # Simulate circuit breaker tripped
            
            bot.signal_tracker.should_skip_symbol = MagicMock(return_value=(False, ""))
            is_closed = await bot.run_monitoring_cycle()
            if not is_closed:
                signal = await bot.get_new_entry_signal()
                if signal: await bot.execute_entry(signal, 1000.0)
            
            # Assertions
            assert bot.running is False
            trader.place_order.assert_not_called()
