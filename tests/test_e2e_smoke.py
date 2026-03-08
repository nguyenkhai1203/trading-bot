import pytest
import asyncio
import json
import time
from unittest.mock import MagicMock, AsyncMock, patch
from src.execution import Trader
from src.infrastructure.repository.database import DataManager
from src.data_manager import MarketDataManager

class TestEndToEndSmoke:
    """
    E2E Smoke Testing (Dry-Run logic).
    Verifies full integration from Signal detection to DB update.
    """

    @pytest.fixture
    async def deps(self, tmp_path):
        db_path = str(tmp_path / "e2e_test.db")
        DataManager._instances = {}
        db = DataManager(db_path)
        await db.initialize()
        await db.add_profile(name="E2E_P1", env="TEST", exchange="BINANCE", label="E2E")
        
        mock_ex = MagicMock()
        mock_ex.name = "BINANCE"
        mock_ex.can_trade = True
        mock_ex.is_spot.return_value = False
        mock_ex.is_tpsl_attached_supported.return_value = True
        mock_ex.fetch_positions = AsyncMock(return_value=[])
        mock_ex.fetch_open_orders = AsyncMock(return_value=[])
        mock_ex.round_qty.side_effect = lambda s, q: round(q, 4)
        mock_ex.fetch_ticker = AsyncMock(return_value={'last': 50000})
        mock_ex.milliseconds.return_value = int(time.time() * 1000)
        mock_ex.ensure_isolated_and_leverage = AsyncMock()
        
        mock_ex.exchange = MagicMock()
        mock_ex.exchange.apiKey = "MOCK_KEY"
        
        mdm = MarketDataManager(db=db, adapters={"BINANCE": mock_ex})
        return db, mock_ex, mdm

    @pytest.mark.asyncio
    async def test_full_cycle_signal_to_order(self, deps):
        db, mock_ex, mdm = deps
        trader = Trader(mock_ex, db, profile_id=1, dry_run=False, data_manager=mdm)
        trader.exchange_name = "BINANCE"
        trader.cooldown_manager = MagicMock()
        trader.cooldown_manager.is_in_cooldown.return_value = False
        trader.cooldown_manager.is_margin_throttled.return_value = False
        
        mock_order = {
            'id': 'E2E_ORD_1', 
            'status': 'open', 
            'average': 50000, 
            'timestamp': int(time.time() * 1000),
            'clientOrderId': 'mock_client_id'
        }
        
        # Mocks for complex internals
        trader._check_min_notional = MagicMock(return_value=(True, "OK", 5000.0))
        trader._execute_with_timestamp_retry = AsyncMock(return_value=mock_order)
        
        # Patch telegram to avoid external calls
        with patch("src.infrastructure.notifications.notification.send_telegram_message", new_callable=AsyncMock), \
             patch("src.order_executor.send_telegram_message", new_callable=AsyncMock), \
             patch("src.config.MAX_DAILY_LOSS_USD", 500, create=True): 
            
            res = await trader.place_order(
                symbol='BTC/USDT',
                side='BUY',
                qty=0.1,
                timeframe='1h',
                price=50000,
                sl=49000,
                tp=52000,
                entry_confidence=0.85,
                signals_used=['RSI_OVERSOLD']
            )
            
            assert res is not None
            assert res['id'] == 'E2E_ORD_1'
            
            # Verify Memory State
            pos_key = trader._get_pos_key('BTC/USDT', '1h')
            assert pos_key in trader.active_positions
            assert trader.active_positions[pos_key]['status'] == 'filled'
            
            # Verify DB Persistence
            db_pos = await db.get_active_positions(profile_id=1)
            assert len(db_pos) == 1
            assert db_pos[0]['symbol'] == 'BTC/USDT'

    @pytest.mark.asyncio
    async def test_emergency_kill_switch(self, deps):
        db, mock_ex, mdm = deps
        # Dry run is safer for kill switch tests
        trader = Trader(mock_ex, db, profile_id=1, dry_run=True, env="TEST", data_manager=mdm)
        trader.exchange_name = "BINANCE"
        
        # Manually seed a position
        pos_key = trader._get_pos_key("BTC/USDT", "1h")
        trader.active_positions[pos_key] = {
            'order_id': 'O1', 'symbol': 'BTC/USDT', 'status': 'filled', 'side': 'BUY', 'qty': 1, 'entry_price': 50000,
            'timeframe': '1h'
        }
        await db.save_position({
            'profile_id': 1, 'pos_key': pos_key, 'symbol': 'BTC/USDT', 'status': 'ACTIVE', 
            'side': 'BUY', 'exchange': 'BINANCE', 'timeframe': '1h', 'entry_price': 50000, 'qty': 1
        })
        
        # Mock close logic
        mock_ex.close_position = AsyncMock(return_value={'price': 50100})
        
        with patch("src.infrastructure.notifications.notification.send_telegram_message", new_callable=AsyncMock):
             # Force close
             await trader.force_close_position(pos_key, reason="EMERGENCY_KILL")
             
             assert len(trader.active_positions) == 0
             db_pos = await db.get_active_positions(profile_id=1)
             assert len(db_pos) == 0
