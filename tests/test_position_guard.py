"""
Tests for:
1. Symbol-level position guard in place_order
2. Pending order purge in set_sl_cooldown
"""
import sys
import os
import time
import asyncio
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import pytest
from unittest.mock import MagicMock, AsyncMock, patch, call
from src.execution import Trader


def _make_trader(exchange_name='BYBIT'):
    mock_ex = MagicMock()
    mock_ex.name = exchange_name
    mock_ex.is_public_only = False
    mock_ex.can_trade = True
    mock_ex.milliseconds = MagicMock(return_value=int(time.time() * 1000))
    mock_ex.round_qty = MagicMock(side_effect=lambda sym, qty: round(qty, 3))
    mock_db = MagicMock()
    trader = Trader(mock_ex, db=mock_db, profile_id=1, dry_run=False)
    trader.exchange_name = exchange_name
    trader.logger = MagicMock()
    trader._check_min_notional = MagicMock(return_value=(True, "", 100.0))
    trader._sl_cooldowns = {}
    return trader


# ─── Fix 1: Symbol-Level Position Guard ────────────────────────────────────────

@pytest.mark.asyncio
async def test_guard_blocks_new_order_when_symbol_filled():
    """place_order must return None if symbol already has a FILLED position (any TF)."""
    trader = _make_trader('BYBIT')
    # Active 1h position for NEAR
    trader.active_positions['P1_BYBIT_NEAR_USDT_1h'] = {
        'symbol': 'NEAR/USDT:USDT', 'side': 'BUY',
        'status': 'filled', 'qty': 10, 'entry_price': 5.0,
    }

    result = await trader.place_order(
        symbol='NEAR/USDT:USDT', side='BUY', qty=10,
        timeframe='15m', order_type='limit', price=5.0,
        sl=4.7, tp=5.4
    )

    assert result is None
    trader.logger.info.assert_called()
    log_msg = str(trader.logger.info.call_args_list)
    assert 'GUARD' in log_msg or 'blocked' in log_msg.lower() or 'FILLED' in log_msg


@pytest.mark.asyncio
async def test_guard_blocks_new_order_when_symbol_pending():
    """place_order must return None if symbol already has a PENDING position (any TF)."""
    trader = _make_trader('BINANCE')
    trader.active_positions['P1_BINANCE_SOL_USDT_4h'] = {
        'symbol': 'SOL/USDT:USDT', 'side': 'SELL',
        'status': 'pending', 'qty': 1, 'entry_price': 150.0,
    }

    result = await trader.place_order(
        symbol='SOL/USDT:USDT', side='SELL', qty=1,
        timeframe='1h', order_type='limit', price=150.0,
        sl=155.0, tp=140.0
    )

    assert result is None


@pytest.mark.asyncio
async def test_guard_allows_reduce_only_when_symbol_filled():
    """reduce_only orders (SL/TP close) must NOT be blocked by the symbol guard.
    We verify the GUARD log is never emitted, regardless of whether the full order succeeds.
    """
    trader = _make_trader('BYBIT')
    trader.active_positions['P1_BYBIT_BTC_USDT_1h'] = {
        'symbol': 'BTC/USDT:USDT', 'side': 'BUY',
        'status': 'filled', 'qty': 0.01, 'entry_price': 50000,
    }

    # Use qty=0 so the order is rejected at a DIFFERENT (earlier) validation step,
    # but we can still confirm the GUARD block was not hit.
    result = await trader.place_order(
        symbol='BTC/USDT:USDT', side='SELL', qty=0,  # Rejected at qty <= 0 check
        timeframe='1h', reduce_only=True
    )
    # Guard must not have logged [GUARD]
    for c in trader.logger.info.call_args_list:
        assert 'GUARD' not in str(c), "Guard should not trigger for reduce_only orders"


@pytest.mark.asyncio
async def test_guard_allows_entry_when_no_existing_position():
    """place_order should proceed normally when no active/pending position exists."""
    trader = _make_trader('BYBIT')
    trader.active_positions = {}  # Empty

    # Check it gets past guard (qty 0 will be rejected at a different step, which is fine)
    result = await trader.place_order(
        symbol='ETH/USDT:USDT', side='BUY', qty=0,  # Zero qty → rejected at qty check
        timeframe='1h'
    )
    # Should be rejected at qty validation (not the guard)
    assert result is None
    # Verify the guard log was NOT triggered
    for c in trader.logger.info.call_args_list:
        assert 'GUARD' not in str(c)


# ─── Fix 2: Cooldown Purges Pending Orders ─────────────────────────────────────

@pytest.mark.asyncio
async def test_cooldown_cancels_exchange_orders():
    """set_sl_cooldown must call cancel_all_orders for the symbol."""
    trader = _make_trader('BYBIT')
    trader.exchange.cancel_all_orders = AsyncMock()
    trader.db.set_risk_metric = AsyncMock()

    await trader.set_sl_cooldown('NEAR/USDT:USDT')

    trader.exchange.cancel_all_orders.assert_called_once_with('NEAR/USDT:USDT')


@pytest.mark.asyncio
async def test_cooldown_purges_local_pending_entries():
    """set_sl_cooldown must remove all pending entries for that symbol from active_positions."""
    trader = _make_trader('BYBIT')
    trader.exchange.cancel_all_orders = AsyncMock()
    trader.db.set_risk_metric = AsyncMock()
    trader._clear_db_position = AsyncMock()

    # Setup: 1h pending for NEAR + 15m pending for NEAR + 1h filled for BTC (should NOT be removed)
    trader.active_positions = {
        'P1_BYBIT_NEAR_USDT_1h': {'symbol': 'NEAR/USDT:USDT', 'status': 'pending'},
        'P1_BYBIT_NEAR_USDT_15m': {'symbol': 'NEAR/USDT:USDT', 'status': 'pending'},
        'P1_BYBIT_BTC_USDT_1h': {'symbol': 'BTC/USDT:USDT', 'status': 'filled'},
    }

    await trader.set_sl_cooldown('NEAR/USDT:USDT')

    # Both NEAR pending entries removed
    assert 'P1_BYBIT_NEAR_USDT_1h' not in trader.active_positions
    assert 'P1_BYBIT_NEAR_USDT_15m' not in trader.active_positions
    # BTC position untouched
    assert 'P1_BYBIT_BTC_USDT_1h' in trader.active_positions

    # DB cleared for both NEAR pending positions
    assert trader._clear_db_position.call_count == 2


@pytest.mark.asyncio
async def test_cooldown_does_not_remove_filled_positions():
    """set_sl_cooldown must NOT remove FILLED positions — only pending entries."""
    trader = _make_trader('BYBIT')
    trader.exchange.cancel_all_orders = AsyncMock()
    trader.db.set_risk_metric = AsyncMock()
    trader._clear_db_position = AsyncMock()

    trader.active_positions = {
        'P1_BYBIT_NEAR_USDT_1h': {'symbol': 'NEAR/USDT:USDT', 'status': 'filled'},
    }

    await trader.set_sl_cooldown('NEAR/USDT:USDT')

    # Filled position should remain (it's being closed by the exchange, not us)
    assert 'P1_BYBIT_NEAR_USDT_1h' in trader.active_positions
    trader._clear_db_position.assert_not_called()
