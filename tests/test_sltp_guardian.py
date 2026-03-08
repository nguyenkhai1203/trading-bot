"""
Tests for scan_sltp_liveness — SL/TP Guardian
Verifies the guardian correctly detects and fixes missing SL/TP orders on exchange.
"""
import sys
import os
import time
import asyncio
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from src.execution import Trader


def _make_trader(exchange_name='BYBIT'):
    mock_ex = MagicMock()
    mock_ex.name = exchange_name
    mock_ex.is_public_only = False
    mock_ex.can_trade = True
    mock_ex.milliseconds = MagicMock(return_value=int(time.time() * 1000))
    mock_db = MagicMock()
    trader = Trader(mock_ex, db=mock_db, profile_id=1, dry_run=False)
    trader.exchange_name = exchange_name
    trader.logger = MagicMock()
    # Reset throttle cache so guardian runs immediately
    trader._pos_action_timestamps = {}
    # Empty exchange pos cache
    trader._last_ex_pos_map = {}
    return trader


@pytest.mark.asyncio
async def test_guardian_protected_position_skipped():
    """Position with both sl_order_id and tp_order_id should be skipped (no action)."""
    trader = _make_trader('BINANCE')
    pos_key = "P1_BINANCE_BTC_USDT_1h"
    trader.active_positions[pos_key] = {
        'symbol': 'BTC/USDT:USDT', 'side': 'BUY',
        'entry_price': 50000, 'qty': 0.01, 'status': 'filled',
        'sl_order_id': 'sl_abc', 'tp_order_id': 'tp_xyz',
        'sl': 48000, 'tp': 54000
    }

    with patch.object(trader, 'recreate_missing_sl_tp', new_callable=AsyncMock) as mock_rec:
        with patch.object(trader, 'force_close_position', new_callable=AsyncMock) as mock_close:
            await trader.scan_sltp_liveness()
            mock_rec.assert_not_called()
            mock_close.assert_not_called()


@pytest.mark.asyncio
async def test_guardian_bybit_attached_sltp_skipped():
    """Bybit position with attached SL/TP in exchange cache should be skipped."""
    trader = _make_trader('BYBIT')
    pos_key = "P1_BYBIT_ETH_USDT_1h"
    trader.active_positions[pos_key] = {
        'symbol': 'ETH/USDT:USDT', 'side': 'BUY',
        'entry_price': 3000, 'qty': 0.1, 'status': 'filled',
        'sl_order_id': None, 'tp_order_id': None,  # No order IDs
        'sl': 2900, 'tp': 3200
    }
    # Simulate Bybit returning attached SL/TP in position cache
    trader._last_ex_pos_map = {
        'ETHUSDT': {'stopLoss': 2900.0, 'takeProfit': 3200.0}
    }

    with patch.object(trader, 'recreate_missing_sl_tp', new_callable=AsyncMock) as mock_rec:
        with patch.object(trader, 'force_close_position', new_callable=AsyncMock) as mock_close:
            await trader.scan_sltp_liveness()
            mock_rec.assert_not_called()
            mock_close.assert_not_called()


@pytest.mark.asyncio
async def test_guardian_detects_missing_sl_recreates():
    """Position missing SL order should trigger recreate_missing_sl_tp(recreate_sl=True)."""
    trader = _make_trader('BINANCE')
    pos_key = "P1_BINANCE_SOL_USDT_1h"
    trader.active_positions[pos_key] = {
        'symbol': 'SOL/USDT:USDT', 'side': 'BUY',
        'entry_price': 150, 'qty': 1.0, 'status': 'filled',
        'sl_order_id': None,        # SL missing!
        'tp_order_id': 'tp_123',    # TP exists
        'sl': 140, 'tp': 165
    }

    with patch.object(trader, 'recreate_missing_sl_tp', new_callable=AsyncMock) as mock_rec:
        mock_rec.return_value = {'sl_recreated': True, 'tp_recreated': False}
        await trader.scan_sltp_liveness()

        mock_rec.assert_called_once()
        call_kwargs = mock_rec.call_args
        assert call_kwargs[1].get('recreate_sl') is True or call_kwargs[0][1] is True
        assert call_kwargs[1].get('recreate_tp') is False or call_kwargs[0][2] is False


@pytest.mark.asyncio
async def test_guardian_detects_missing_tp_recreates():
    """Position missing TP order should trigger recreate_missing_sl_tp(recreate_tp=True)."""
    trader = _make_trader('BYBIT')
    pos_key = "P1_BYBIT_XRP_USDT_1h"
    trader.active_positions[pos_key] = {
        'symbol': 'XRP/USDT:USDT', 'side': 'SELL',
        'entry_price': 0.5, 'qty': 100, 'status': 'filled',
        'sl_order_id': 'sl_456',    # SL exists
        'tp_order_id': None,        # TP missing!
        'sl': 0.55, 'tp': 0.44
    }
    trader._last_ex_pos_map = {}  # No attached SL/TP either

    with patch.object(trader, 'recreate_missing_sl_tp', new_callable=AsyncMock) as mock_rec:
        mock_rec.return_value = {'sl_recreated': False, 'tp_recreated': True}
        await trader.scan_sltp_liveness()

        mock_rec.assert_called_once()
        call_kwargs = mock_rec.call_args
        assert call_kwargs[1].get('recreate_tp') is True or call_kwargs[0][2] is True
        assert call_kwargs[1].get('recreate_sl') is False or call_kwargs[0][1] is False


@pytest.mark.asyncio
async def test_guardian_emergency_close_no_protection_high_pnl():
    """
    Position with NO SL/TP at all and PnL >= 8% should trigger emergency close
    (not just recreate).
    """
    trader = _make_trader('BYBIT')
    pos_key = "P1_BYBIT_BTC_USDT_1h"
    entry = 50000.0
    trader.active_positions[pos_key] = {
        'symbol': 'BTC/USDT:USDT', 'side': 'BUY',
        'entry_price': entry, 'qty': 0.01, 'status': 'filled',
        'sl_order_id': None, 'tp_order_id': None,
        'sl': None, 'tp': None
    }
    trader._last_ex_pos_map = {}  # No Bybit attached either

    # Price pumped +10% — above 8% threshold
    # Mock directly on exchange attribute (guardian calls self.exchange.fetch_ticker directly)
    trader.exchange.fetch_ticker = AsyncMock(return_value={'last': entry * 1.10, 'close': entry * 1.10})

    with patch.object(trader, 'force_close_position', new_callable=AsyncMock) as mock_close:
        with patch.object(trader, 'recreate_missing_sl_tp', new_callable=AsyncMock) as mock_rec:
            await trader.scan_sltp_liveness()

            mock_close.assert_called_once()
            call_kwargs = mock_close.call_args
            reason = call_kwargs[1].get('reason') or (call_kwargs[0][1] if len(call_kwargs[0]) > 1 else '')
            assert "Guardian" in reason
            mock_rec.assert_not_called()


@pytest.mark.asyncio
async def test_guardian_no_emergency_when_pnl_below_threshold():
    """
    Position with NO SL/TP but PnL < 8% should not emergency close — just recreate.
    """
    trader = _make_trader('BYBIT')
    pos_key = "P1_BYBIT_ADA_USDT_1h"
    entry = 0.40
    trader.active_positions[pos_key] = {
        'symbol': 'ADA/USDT:USDT', 'side': 'BUY',
        'entry_price': entry, 'qty': 100, 'status': 'filled',
        'sl_order_id': None, 'tp_order_id': None,
        'sl': None, 'tp': None
    }
    trader._last_ex_pos_map = {}

    # Price up only 3% — below 8% threshold (SLTP_GUARDIAN_EMERGENCY_PCT = 0.08)
    trader.exchange.fetch_ticker = AsyncMock(return_value={'last': entry * 1.03, 'close': entry * 1.03})

    with patch.object(trader, 'force_close_position', new_callable=AsyncMock) as mock_close:
        with patch.object(trader, 'recreate_missing_sl_tp', new_callable=AsyncMock) as mock_rec:
            mock_rec.return_value = {}
            await trader.scan_sltp_liveness()

            mock_close.assert_not_called()
            mock_rec.assert_called_once()


@pytest.mark.asyncio
async def test_guardian_throttle_skips_recent_check():
    """Guardian should skip position that was checked < 60s ago."""
    trader = _make_trader('BINANCE')
    pos_key = "P1_BINANCE_LINK_USDT_1h"
    trader.active_positions[pos_key] = {
        'symbol': 'LINK/USDT:USDT', 'side': 'BUY',
        'entry_price': 20, 'qty': 1, 'status': 'filled',
        'sl_order_id': None, 'tp_order_id': None,
        'sl': None, 'tp': None
    }
    # Simulate recent check (30s ago)
    trader._pos_action_timestamps[f"{pos_key}_sltp_guardian"] = (time.time() - 30) * 1000

    with patch.object(trader, 'recreate_missing_sl_tp', new_callable=AsyncMock) as mock_rec:
        with patch.object(trader, 'force_close_position', new_callable=AsyncMock) as mock_close:
            await trader.scan_sltp_liveness()
            mock_rec.assert_not_called()
            mock_close.assert_not_called()


@pytest.mark.asyncio
async def test_guardian_dry_run_skipped():
    """Guardian should do nothing in dry_run mode."""
    mock_ex = MagicMock()
    mock_ex.name = 'BYBIT'
    mock_ex.is_public_only = False
    trader = Trader(mock_ex, db=MagicMock(), profile_id=1, dry_run=True)
    trader.exchange_name = 'BYBIT'
    trader.active_positions['P1_BYBIT_ETH_USDT_1h'] = {
        'symbol': 'ETH/USDT:USDT', 'side': 'BUY',
        'entry_price': 3000, 'qty': 0.1, 'status': 'filled',
        'sl_order_id': None, 'tp_order_id': None,
    }

    with patch.object(trader, 'recreate_missing_sl_tp', new_callable=AsyncMock) as mock_rec:
        await trader.scan_sltp_liveness()
        mock_rec.assert_not_called()
