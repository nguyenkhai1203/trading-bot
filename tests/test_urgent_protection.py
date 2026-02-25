import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from execution import Trader

@pytest.mark.asyncio
async def test_urgent_protection_tp_passed_buy():
    """
    Scenario: BUY position, price passes TP. 
    Verify force_close_position is called.
    """
    mock_ex = MagicMock()
    mock_ex.name = 'BINANCE'
    mock_ex.is_public_only = False
    mock_ex.can_trade = True
    mock_ex.fetch_ticker = AsyncMock(return_value={'last': 105})
    mock_ex.milliseconds = MagicMock(return_value=1000)
    
    trader = Trader(mock_ex, dry_run=False)
    pos_key = "BINANCE_BTC/USDT_1h"
    trader.active_positions[pos_key] = {
        'symbol': 'BTC/USDT',
        'side': 'BUY',
        'qty': 1.0,
        'entry_price': 100,
        'tp': 102, # Hit! Price is 105
        'sl': 95,
        'status': 'filled'
    }
    
    with patch.object(trader, 'force_close_position', new_callable=AsyncMock) as mock_close:
        with patch.object(trader, '_save_positions'):
            result = await trader.recreate_missing_sl_tp(pos_key, recreate_tp=True)
            
            assert result['status'] == 'closed'
            mock_close.assert_called_once()
            assert "TP passed" in mock_close.call_args[1]['reason']

@pytest.mark.asyncio
async def test_urgent_protection_sl_passed_buy():
    """
    Scenario: BUY position, price drops below SL.
    Verify force_close_position is called.
    """
    mock_ex = MagicMock()
    mock_ex.name = 'BINANCE'
    mock_ex.is_public_only = False
    mock_ex.can_trade = True
    mock_ex.fetch_ticker = AsyncMock(return_value={'last': 90})
    mock_ex.milliseconds = MagicMock(return_value=1000)
    
    trader = Trader(mock_ex, dry_run=False)
    pos_key = "BINANCE_BTC/USDT_1h"
    trader.active_positions[pos_key] = {
        'symbol': 'BTC/USDT',
        'side': 'BUY',
        'qty': 1.0,
        'entry_price': 100,
        'tp': 110,
        'sl': 95, # Hit! Price is 90
        'status': 'filled'
    }
    
    with patch.object(trader, 'force_close_position', new_callable=AsyncMock) as mock_close:
        with patch.object(trader, '_save_positions'):
            result = await trader.recreate_missing_sl_tp(pos_key, recreate_sl=True)
            
            assert result['status'] == 'closed'
            mock_close.assert_called_once()
            assert "SL passed" in mock_close.call_args[1]['reason']

@pytest.mark.asyncio
async def test_urgent_protection_tp_passed_sell():
    """
    Scenario: SELL position, price drops below TP.
    """
    mock_ex = MagicMock()
    mock_ex.name = 'BINANCE'
    mock_ex.is_public_only = False
    mock_ex.can_trade = True
    mock_ex.fetch_ticker = AsyncMock(return_value={'last': 90}) 
    mock_ex.milliseconds = MagicMock(return_value=1000)
    
    trader = Trader(mock_ex, dry_run=False)
    pos_key = "BINANCE_BTC/USDT_1h"
    trader.active_positions[pos_key] = {
        'symbol': 'BTC/USDT',
        'side': 'SELL',
        'qty': 1.0,
        'entry_price': 100,
        'tp': 95, # Hit! Price is 90
        'sl': 105,
        'status': 'filled'
    }
    
    with patch.object(trader, 'force_close_position', new_callable=AsyncMock) as mock_close:
        with patch.object(trader, '_save_positions'):
            result = await trader.recreate_missing_sl_tp(pos_key, recreate_tp=True)
            
            assert result['status'] == 'closed'
            mock_close.assert_called_once()
            assert "TP passed" in mock_close.call_args[1]['reason']
