import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from bot import TradingBot

@pytest.mark.asyncio
async def test_confidence_competition_new_entry():
    """
    Scenario: Two bots for the same symbol find signals.
    Highest confidence signal should be picked.
    """
    mock_trader = MagicMock()
    mock_trader.exchange_name = 'BINANCE'
    mock_trader._get_lock = MagicMock(return_value=AsyncMock().__aenter__.return_value)
    mock_trader.has_any_symbol_position = AsyncMock(return_value=False)
    mock_trader.active_positions = {}
    mock_trader.pending_orders = {}
    
    mock_dm = MagicMock()
    
    bot_5m = TradingBot('BTC/USDT', '5m', mock_dm, mock_trader)
    bot_1h = TradingBot('BTC/USDT', '1h', mock_dm, mock_trader)
    
    # Mock get_new_entry_signal
    sig_5m = {'side': 'BUY', 'confidence': 0.4, 'last_row': MagicMock()}
    sig_1h = {'side': 'BUY', 'confidence': 0.7, 'last_row': MagicMock()}
    
    bot_5m.get_new_entry_signal = AsyncMock(return_value=sig_5m)
    bot_1h.get_new_entry_signal = AsyncMock(return_value=sig_1h)
    bot_5m.execute_entry = AsyncMock()
    bot_1h.execute_entry = AsyncMock()
    
    # Simulate the coordination logic from main loop
    group_bots = [bot_5m, bot_1h]
    symbol = 'BTC/USDT'
    
    async def process_symbol_group(trader, symbol, group_bots):
        # Collect signals
        signals = await asyncio.gather(*[b.get_new_entry_signal() for b in group_bots])
        valid_signals = []
        for idx, sig in enumerate(signals):
            if sig:
                valid_signals.append((group_bots[idx], sig))
        
        if valid_signals:
            # Confidence Competition
            best_bot, best_signal = max(valid_signals, key=lambda x: x[1]['confidence'])
            await best_bot.execute_entry(best_signal, 1000.0)

    await process_symbol_group(mock_trader, symbol, group_bots)
    
    # Assertions
    bot_1h.execute_entry.assert_called_once()
    bot_5m.execute_entry.assert_not_called()

@pytest.mark.asyncio
async def test_pending_replacement_logic():
    """
    Scenario: Existing pending order on 1h (conf 0.5).
    New signal on 5m (conf 0.8) arrives.
    1h should be cancelled and 5m entered.
    """
    mock_trader = MagicMock()
    mock_trader.exchange_name = 'BINANCE'
    mock_trader._get_lock = MagicMock(return_value=AsyncMock().__aenter__.return_value)
    mock_trader._get_pos_key = MagicMock(side_effect=lambda s, t: f"BINANCE_{s}_{t}")
    mock_trader.cancel_pending_order = AsyncMock()
    
    # Setup existing pending order
    pos_key_1h = "BINANCE_BTC/USDT_1h"
    mock_trader.active_positions = {
        pos_key_1h: {
            'symbol': 'BTC/USDT',
            'status': 'pending',
            'entry_confidence': 0.5,
            'timeframe': '1h'
        }
    }
    
    mock_dm = MagicMock()
    
    bot_5m = TradingBot('BTC/USDT', '5m', mock_dm, mock_trader)
    bot_1h = TradingBot('BTC/USDT', '1h', mock_dm, mock_trader)
    
    sig_5m = {'side': 'BUY', 'confidence': 0.8, 'last_row': MagicMock()}
    sig_1h = None # No new signal or same signal
    
    bot_5m.get_new_entry_signal = AsyncMock(return_value=sig_5m)
    bot_1h.get_new_entry_signal = AsyncMock(return_value=sig_1h)
    bot_5m.execute_entry = AsyncMock()
    
    group_bots = [bot_5m, bot_1h]
    symbol = 'BTC/USDT'
    
    # Coordination Logic (Snippet from main)
    async def process_symbol_group(trader, symbol, group_bots):
        existing_pending_key = None
        existing_pending_pos = None
        has_filled = False
        
        for key, pos in trader.active_positions.items():
            if pos.get('symbol') == symbol:
                if pos.get('status') == 'filled':
                    has_filled = True
                    break
                elif pos.get('status') == 'pending':
                    existing_pending_key = key
                    existing_pending_pos = pos

        if has_filled: return

        signals = await asyncio.gather(*[b.get_new_entry_signal() for b in group_bots])
        valid_signals = []
        for idx, sig in enumerate(signals):
            if sig:
                valid_signals.append((group_bots[idx], sig))
        
        if not valid_signals: return
        
        best_bot, best_signal = max(valid_signals, key=lambda x: x[1]['confidence'])
        
        if existing_pending_pos:
            ex_conf = existing_pending_pos.get('entry_confidence', 0)
            new_conf = best_signal['confidence']
            if new_conf > (ex_conf + 0.05):
                await trader.cancel_pending_order(existing_pending_key, reason="Better signal")
                await best_bot.execute_entry(best_signal, 1000.0)
            else:
                return
        else:
            await best_bot.execute_entry(best_signal, 1000.0)

    await process_symbol_group(mock_trader, symbol, group_bots)
    
    mock_trader.cancel_pending_order.assert_called_once_with(pos_key_1h, reason="Better signal")
    bot_5m.execute_entry.assert_called_once()
