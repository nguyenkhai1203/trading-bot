import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock
from bot import TradingBot
from adapters.bybit_adapter import BybitAdapter
import config as config

@pytest.mark.asyncio
async def test_bot_limit_order_sltp_calculation():
    """
    Test that bot.py calculates SL and TP relative to the *Limit Entry Target* 
    and not the current Market Price to avoid Bybit SL/TP rejection overlaps.
    """
    mock_strategy = MagicMock()
    # Mock ATR percentages: SL 1.5%, TP 3%
    mock_strategy.get_dynamic_risk_params.return_value = (0.015, 0.03)
    mock_strategy.get_sizing_tier.return_value = {'leverage': 5, 'cost_usdt': 10}
    
    mock_risk_manager = MagicMock()
    mock_risk_manager.calculate_size_by_cost.return_value = 1.0
    
    mock_trader = AsyncMock()
    
    bot = TradingBot('BTC/USDT:USDT', '15m', MagicMock(), mock_trader, mock_risk_manager, MagicMock())
    bot.strategy = mock_strategy
    bot.risk_manager = mock_risk_manager
    bot.trader = mock_trader
    
    # Mock Market Data
    class FakeRow:
        def __init__(self):
            self.data = {'close': 100.0}
        def __getitem__(self, key):
            return self.data[key]
        def to_dict(self):
            return self.data
            
    signal_data = {
        'side': 'BUY',
        'confidence': 0.8,
        'last_row': FakeRow(),
        'comment': ''
    }
    
    # Enable Limit Orders
    config.USE_LIMIT_ORDERS = True
    config.PATIENCE_ENTRY_PCT = 0.01 # 1% discount
    
    # Run the entry execution
    await bot.execute_entry(signal_data, equity=100)
    
    # Assert
    mock_trader.place_order.assert_called_once()
    call_kwargs = mock_trader.place_order.call_args.kwargs
    
    # Market Price = 100
    # Entry Limit Target = 100 - 1% = 99.0
    # SL = 99.0 - 1.5% = 97.515
    # TP = 99.0 + 3.0% = 101.97
    
    assert call_kwargs['order_type'] == 'limit'
    assert call_kwargs['price'] == 99.0
    assert round(call_kwargs['sl'], 3) == 97.515
    assert round(call_kwargs['tp'], 2) == 101.97

@pytest.mark.asyncio
async def test_bybit_adapter_validation_error_returns_false():
    """
    Test that when Bybit rejects an SL/TP attachment with a '10001' validation error 
    (SL too close), the adapter returns sl_set=False so the Guardian can retry later 
    instead of falsely assuming it was attached.
    """
    mock_exchange = AsyncMock()
    mock_exchange.id = 'bybit'
    # Simulate Bybit throwing a validation error
    mock_exchange.privatePostV5PositionTradingStop.side_effect = Exception("Bybit error: 10001 - takeprofit and stoploss cannot be higher than current price")
    mock_exchange.market.return_value = {'id': 'BTCUSDT'}
    
    adapter = BybitAdapter(mock_exchange)
    adapter._fetch_and_cache_position_mode = AsyncMock(return_value='MergedSingle')
    
    result = await adapter.set_position_sl_tp('BTC/USDT:USDT', 'BUY', sl=50000)
    
    # MUST return false
    assert result['sl_set'] is False
    assert result['tp_set'] is False

@pytest.mark.asyncio
async def test_bybit_adapter_precision_string_formatting():
    """
    Test that BybitAdapter.create_order correctly translates float SL/TP into 
    precise strings to bypass Bybit's strict JSON validation.
    """
    mock_exchange = AsyncMock()
    mock_exchange.id = 'bybit'
    mock_exchange.price_to_precision = MagicMock(side_effect=lambda sym, p: f"{p:.2f}")
    
    adapter = BybitAdapter(mock_exchange)
    adapter._fetch_and_cache_position_mode = AsyncMock(return_value='MergedSingle')
    
    await adapter.create_order(
        'ETH/USDT:USDT', 'limit', 'buy', 1.0, 3000.0, 
        params={'stopLoss': 2900.123456, 'takeProfit': 3200.5}
    )
    
    # Verify what got sent to the exchange
    call_args, call_kwargs = mock_exchange.create_order.call_args
    passed_params = call_args[5]
    
    assert passed_params['stopLoss'] == "2900.12" # Properly formatted string
    assert passed_params['takeProfit'] == "3200.50"
    assert passed_params['tpslMode'] == 'Full'
