import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
import pandas as pd
import numpy as np
from bot import TradingBot, BalanceTracker

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
        trader.is_in_cooldown = MagicMock(return_value=False)
        trader.place_order = AsyncMock(return_value={'id': 'order_1', 'status': 'open'})
        trader.cancel_pending_order = AsyncMock(return_value=True)
        trader.verify_symbol_state = AsyncMock(return_value=None)
        trader.adjust_sl_tp_for_profit_lock = AsyncMock(return_value=None)
        
        balance_tracker = BalanceTracker()
        balance_tracker.update_balance('BINANCE', 1000.0)
        
        return {
            'data_manager': data_manager,
            'trader': trader,
            'balance_tracker': balance_tracker
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
            mock_strat.sl_pct = 0.02
            mock_strat.tp_pct = 0.04
            
            bot = TradingBot('BTC/USDT', '1h', dm, trader, balance_tracker=bt)
            
            # Mock strategy to return a BUY signal
            mock_strat.get_signal = MagicMock(return_value={
                'side': 'BUY', 
                'confidence': 0.8, 
                'comment': 'Strong Bullish (RSI_oversold)',
                'snapshot': {}
            })
            
            # Mock data manager to return data
            df = pd.DataFrame({'close': [50000.0], 'high': [50100.0], 'low': [49900.0]})
            dm.get_data.return_value = df
            dm.get_data_with_features.return_value = df
            
            # Mock risk manager calculation
            bot.risk_manager.calculate_sl_tp = MagicMock(return_value=(49000.0, 52000.0))
            bot.risk_manager.calculate_size_by_cost = MagicMock(return_value=0.01)

            # Mock exchange market for min cost
            trader.exchange.market.return_value = {'limits': {'cost': {'min': 5.0}}}

            # Execute step
            with patch('bot.send_telegram_message', AsyncMock()):
                await bot.run_step()
                
            # Assertions
            trader.place_order.assert_called_once()
            # Reservation should be released in finally block
            assert bt.balances['BINANCE']['reserved'] == 0.0

    @pytest.mark.asyncio
    async def test_run_step_duplicate_prevention(self, mock_components):
        """Verify that new order is skipped if position already exists."""
        dm = mock_components['data_manager']
        trader = mock_components['trader']
        bt = mock_components['balance_tracker']
        
        with patch('bot.WeightedScoringStrategy'):
            bot = TradingBot('BTC/USDT', '1h', dm, trader, balance_tracker=bt)
            
            # Mock data manager to return data
            df = pd.DataFrame({'close': [50000.0]})
            dm.get_data.return_value = df
            
            # Mock existing position
            trader.has_any_symbol_position = AsyncMock(return_value=True)
            
            # Execute step
            await bot.run_step()
            
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
            bot = TradingBot('BTC/USDT', '1h', dm, trader, balance_tracker=bt)
            pos_key = 'BINANCE_BTC_USDT_1h'
            
            # Mock a pending order
            trader.pending_orders[pos_key] = {'side': 'BUY', 'price': 50000.0}
            
            # Mock data
            df = pd.DataFrame({'close': [50000.0]})
            dm.get_data.return_value = df
            dm.get_data_with_features.return_value = df
            
            # Mock strategy to return a SELL signal (reversal)
            mock_strat.get_signal = MagicMock(return_value={'side': 'SELL', 'confidence': 0.8})
            
            # Execute step
            with patch('bot.send_telegram_message', AsyncMock()):
                await bot.run_step()
                
            # Assertions
            trader.cancel_pending_order.assert_called_once()
            assert "Signal reversed to SELL" in trader.cancel_pending_order.call_args[1].get('reason', '')

    @pytest.mark.asyncio
    async def test_run_step_circuit_breaker(self, mock_components):
        """Verify that circuit breaker stops trading."""
        dm = mock_components['data_manager']
        trader = mock_components['trader']
        bt = mock_components['balance_tracker']
        
        with patch('bot.WeightedScoringStrategy'):
            bot = TradingBot('BTC/USDT', '1h', dm, trader, balance_tracker=bt)
            
            # Execute step with circuit breaker flag
            await bot.run_step(circuit_breaker_triggered=True)
            
            # Assertions
            assert bot.running is False
            dm.get_data.assert_not_called()
