import sys
import os
import asyncio
import time
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

# Add src to sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from adapters.bybit_adapter import BybitAdapter
from execution import Trader

@pytest.mark.asyncio
async def test_bybit_adapter_promotes_sltp():
    """Verify BybitAdapter.fetch_positions promotes stopLoss and takeProfit to top-level."""
    mock_ccxt = MagicMock()
    # Mock raw Bybit V5 response
    mock_ccxt.privateGetV5PositionList = AsyncMock(return_value={
        'result': {
            'list': [
                {
                    'symbol': 'BTCUSDT',
                    'size': '0.001',
                    'side': 'Buy',
                    'avgPrice': '50000',
                    'leverage': '10',
                    'unrealisedPnl': '10.5',
                    'stopLoss': '48000',
                    'takeProfit': '55000'
                }
            ]
        }
    })
    
    adapter = BybitAdapter(exchange_client=mock_ccxt)
    positions = await adapter.fetch_positions()
    
    assert len(positions) == 1
    p = positions[0]
    assert p['symbol'] == 'BTCUSDT'
    # Verification of promotion
    assert p['stopLoss'] == 48000.0
    assert p['takeProfit'] == 55000.0
    assert p['contracts'] == 0.001

@pytest.mark.asyncio
async def test_reconcile_bybit_symbol_normalization():
    """
    Verify reconcile_positions correctly matches Bybit symbols and 
    preserves 'attached' SL/TP status.
    """
    mock_ex = MagicMock()
    mock_ex.name = 'BYBIT'
    mock_ex.is_public_only = False
    mock_ex.can_trade = True
    mock_ex.milliseconds = MagicMock(return_value=int(time.time() * 1000))
    # Adapter method mocks
    mock_ex.fetch_positions = AsyncMock(return_value=[
        {
            'symbol': 'BTCUSDT',
            'contracts': 0.001,
            'side': 'BUY',
            'entryPrice': 50000,
            'stopLoss': 48000.0,
            'takeProfit': 55000.0
        }
    ])
    mock_ex.fetch_open_orders = AsyncMock(return_value=[])
    
    trader = Trader(mock_ex, db=MagicMock(), profile_id=1, dry_run=False)
    trader.exchange_name = 'BYBIT'
    
    # Local position with CCXT-style symbol
    pos_key = "P1_BYBIT_BTC_USDT_1h"
    trader.active_positions[pos_key] = {
        'symbol': 'BTC/USDT:USDT',
        'side': 'BUY',
        'entry_price': 50000,
        'qty': 0.001,
        'status': 'filled',
        'sl_order_id': None, # Missing locally
        'tp_order_id': None,
        'sl': 48000,
        'tp': 55000
    }
    
    # Run reconciliation
    await trader.reconcile_positions()
    
    # Verify the position now has 'attached' status instead of being wiped/left None
    updated_pos = trader.active_positions[pos_key]
    assert updated_pos['sl_order_id'] == 'attached'
    assert updated_pos['tp_order_id'] == 'attached'
    assert updated_pos['sl'] == 48000.0

@pytest.mark.asyncio
async def test_reconcile_bybit_updates_sl_tp_from_exchange():
    """Verify reconcile_positions updates local SL/TP prices from Bybit 'attached' values."""
    mock_ex = MagicMock()
    mock_ex.name = 'BYBIT'
    mock_ex.is_public_only = False
    mock_ex.can_trade = True
    mock_ex.milliseconds = MagicMock(return_value=int(time.time() * 1000))
    
    # Exchange has different values than local state
    mock_ex.fetch_positions = AsyncMock(return_value=[
        {
            'symbol': 'ETHUSDT',
            'contracts': 0.1,
            'side': 'BUY',
            'entryPrice': 3000,
            'stopLoss': 2850.0, # Updated on exchange
            'takeProfit': 3300.0
        }
    ])
    mock_ex.fetch_open_orders = AsyncMock(return_value=[])
    
    trader = Trader(mock_ex, db=MagicMock(), profile_id=1, dry_run=False)
    trader.exchange_name = 'BYBIT'
    
    pos_key = "P1_BYBIT_ETH_USDT_1h"
    trader.active_positions[pos_key] = {
        'symbol': 'ETH/USDT:USDT',
        'side': 'BUY',
        'entry_price': 3000,
        'qty': 0.1,
        'status': 'filled',
        'sl_order_id': 'attached',
        'tp_order_id': 'attached',
        'sl': 2900, # Old value
        'tp': 3200
    }
    
    await trader.reconcile_positions()
    
    updated_pos = trader.active_positions[pos_key]
    assert updated_pos['sl'] == 2850.0
    assert updated_pos['tp'] == 3300.0
