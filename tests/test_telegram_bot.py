import pytest
import asyncio
import os
import json
from unittest.mock import MagicMock, AsyncMock, patch, mock_open

# We must mock environment variables BEFORE importing telegram_bot
os.environ["TELEGRAM_TOKEN"] = "dummy_token"
os.environ["TELEGRAM_CHAT_ID"] = "123456"

from telegram_bot import get_status_message, get_summary_message
import telegram_bot

class TestTelegramBot:
    
    @pytest.fixture(autouse=True)
    def setup_mocks(self):
        """Setup global mocks within telegram_bot module to isolate tests."""
        # Mock Data Manager Adapters
        self.mock_binance = MagicMock()
        self.mock_binance.exchange.apiKey = "fake_key"
        self.mock_binance.is_public_only = False
        self.mock_binance.fetch_positions = AsyncMock(return_value=[])
        self.mock_binance.fetch_open_orders = AsyncMock(return_value=[])
        
        telegram_bot.db = MagicMock()
        telegram_bot.data_manager = MagicMock()
        telegram_bot.data_manager.adapters = {'BINANCE': self.mock_binance}
        
        # Mock Fetch Ticker
        telegram_bot.data_manager.fetch_ticker = AsyncMock(return_value={'last': 50000.0})
        
        # Mock Traders
        self.mock_trader = MagicMock()
        self.mock_trader.db = MagicMock()
        self.mock_trader.db.get_risk_metric = AsyncMock(return_value=1000.0)
        self.mock_trader._normalize_symbol.side_effect = lambda x: x # pass through
        self.mock_trader._get_pos_key.side_effect = lambda sym, tf: f"BINANCE_{sym.replace('/', '_')}_{tf}"
        self.mock_trader.exchange = MagicMock()
        self.mock_trader.exchange.fetch_positions = AsyncMock(return_value=[])
        self.mock_trader.exchange.fetch_open_orders = AsyncMock(return_value=[])
        self.mock_trader.exchange_name = 'BINANCE'
        
        telegram_bot.traders = {1: self.mock_trader}
        telegram_bot.profiles = [{'id': 1, 'name': 'BINANCE', 'environment': 'LIVE'}]
        telegram_bot.get_total_equity = AsyncMock(return_value=1500.0)
        
        yield
        
        # Cleanup
        telegram_bot.data_manager.adapters = {}

    @pytest.mark.asyncio
    @patch('os.path.exists', return_value=True)
    @patch('os.path.getsize', return_value=100)
    async def test_get_status_message_live_positions(self, mock_getsize, mock_exists):
        """Test the /status formatter with live mocked positions from the exchange adapter."""
        
        # 1. Provide live mock positions via fresh exchange fetch
        self.mock_trader.exchange.fetch_positions.return_value = [
            {
                'symbol': 'BTC/USDT',
                'contracts': 1.0,
                'side': 'LONG',
                'entryPrice': 40000.0,
                'leverage': 10
            }
        ]
        # Keep local memory for SL/TP lookup
        self.mock_trader.active_positions = {
            'BINANCE_BTC_USDT_1h': {
                'symbol': 'BTC/USDT',
                'tp': 45000.0,
                'sl': 39000.0
            }
        }
        self.mock_trader.dry_run = False
        
        # 2. Mock DB risk metric to prevent errors
        telegram_bot.db.get_risk_metric = AsyncMock(return_value=1000.0)
        
        msg = await get_status_message()
            
        # 3. Assert correct formatting
        assert "BOT STATUS" in msg
        assert "BINANCE" in msg
        assert "BTC/USDT" in msg
        assert "40000.00" in msg # Entry price
        assert "50000.00" in msg # Current price (from mocked ticker)
        assert "TP: 45000.00" in msg
        assert "SL: 39000.00" in msg

    @pytest.mark.asyncio
    @patch('os.path.exists', return_value=True)
    @patch('os.path.getsize', return_value=100)
    async def test_get_status_message_pending_orders(self, mock_getsize, mock_exists):
        """Test the /status formatter handles pending limit orders correctly."""
        
        # Live positions empty, but pending orders exist on exchange
        self.mock_trader.exchange.fetch_positions.return_value = []
        self.mock_trader.exchange.fetch_open_orders.return_value = [
            {
                'symbol': 'ETH/USDT',
                'side': 'BUY',
                'amount': 2.0,
                'price': 2000.0,
                'type': 'limit'
            }
        ]
        self.mock_trader.dry_run = False
        
        # No local data reading needed, just mock DB again
        telegram_bot.db.get_risk_metric = AsyncMock(return_value=1000.0)
        
        msg = await get_status_message()
            
        # Assert format
        assert "PENDING" in msg
        assert "ETH/USDT" in msg
        assert "2000.00" in msg

    @pytest.mark.asyncio
    async def test_get_summary_message(self):
        """Test the /summary formatter aggregates PnL from SQLite db."""
        
        trade_history = [
            {
                "symbol": "BTC_USDT",
                "result": "WIN",
                "pnl_pct": 5.0,
                "pnl_usdt": 100.0,
                "exit_time": 1704110400000  # 2024-01-01T12:00:00
            },
            {
                "symbol": "ETH_USDT",
                "result": "LOSS",
                "pnl_pct": -2.0,
                "pnl_usdt": -40.0,
                "exit_time": 1704196800000  # 2024-01-02T12:00:00
            }
        ]
        
        telegram_bot.db.get_trade_history = AsyncMock(return_value=trade_history)
        
        msg = await get_summary_message('all')
            
        assert "ALL TIME" in msg
        assert "Total Trades: 2" in msg
        assert "Wins: 1" in msg
        assert "Loss" in msg
        assert "Win Rate: *50.0%*" in msg
        assert "Net P&L: *+3.00%* ($+60.00)" in msg

    @pytest.mark.asyncio
    async def test_get_summary_message_no_history(self):
        """Test summary handles empty db history gracefully."""
        
        telegram_bot.db.get_trade_history = AsyncMock(return_value=[])
        
        msg = await get_summary_message('all')
            
        assert "No trades found" in msg
