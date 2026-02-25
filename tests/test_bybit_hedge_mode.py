import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from adapters.bybit_adapter import BybitAdapter

@pytest.fixture(autouse=True)
def isolate_env():
    """Ensure tests aren't affected by local .env BYBIT_POS_MODE."""
    with patch('os.getenv', return_value=None):
        yield

@pytest.fixture
def mock_ccxt_bybit():
    exch = MagicMock()
    exch.id = 'bybit'
    exch.create_order = AsyncMock(return_value={'id': 'order_123'})
    exch.fetch_position_mode = AsyncMock(return_value={'mode': 'hedged', 'hedged': True})
    exch.privateGetV5PositionList = AsyncMock(return_value={'result': {'list': [{'positionIdx': 1}]}})
    exch.privatePostV5PositionTradingStop = AsyncMock(return_value={'retCode': 0})
    exch.market = MagicMock(return_value={'id': 'BTCUSDT'})
    exch.price_to_precision = MagicMock(side_effect=lambda symbol, price: str(price))
    exch.fetch_ticker = AsyncMock(return_value={
        'symbol': 'BTC/USDT',
        'last': 60000.0,
        'mark': 60000.0,
        'info': {'markPrice': '60000.0'}
    })
    return exch

@pytest.fixture
def adapter(mock_ccxt_bybit):
    return BybitAdapter(mock_ccxt_bybit)

def get_params(mock_func):
    """Helper to extract params from mock call args."""
    args, kwargs = mock_func.call_args
    # In adapter: await func(symbol, type, side, amount, price, combined_params)
    # symbol=0, type=1, side=2, amount=3, price=4, params=5
    if len(args) > 5:
        return args[5]
    return kwargs.get('params', {})

@pytest.mark.asyncio
async def test_hedge_mode_mapping_buy(adapter, mock_ccxt_bybit):
    adapter._position_mode = 'BothSide'
    await adapter.create_order('BTC/USDT', 'market', 'BUY', 0.001)
    params = get_params(mock_ccxt_bybit.create_order)
    assert params['positionIdx'] == 1

@pytest.mark.asyncio
async def test_hedge_mode_mapping_sell(adapter, mock_ccxt_bybit):
    adapter._position_mode = 'BothSide'
    await adapter.create_order('BTC/USDT', 'market', 'SELL', 0.001)
    params = get_params(mock_ccxt_bybit.create_order)
    assert params['positionIdx'] == 2

@pytest.mark.asyncio
async def test_one_way_mode_mapping(adapter, mock_ccxt_bybit):
    adapter._position_mode = 'MergedSingle'
    await adapter.create_order('BTC/USDT', 'market', 'BUY', 0.001)
    params = get_params(mock_ccxt_bybit.create_order)
    assert 'positionIdx' not in params

@pytest.mark.asyncio
async def test_hedge_mode_close_buy(adapter, mock_ccxt_bybit):
    adapter._position_mode = 'BothSide'
    # Close BUY (Long) -> Send SELL with reduceOnly
    await adapter.create_order('BTC/USDT', 'market', 'SELL', 0.001, params={'reduceOnly': True})
    params = get_params(mock_ccxt_bybit.create_order)
    assert params['positionIdx'] == 1
    assert params['reduceOnly'] is True

@pytest.mark.asyncio
async def test_hedge_mode_close_sell(adapter, mock_ccxt_bybit):
    adapter._position_mode = 'BothSide'
    # Close SELL (Short) -> Send BUY with reduceOnly
    await adapter.create_order('BTC/USDT', 'market', 'BUY', 0.001, params={'reduceOnly': True})
    params = get_params(mock_ccxt_bybit.create_order)
    assert params['positionIdx'] == 2
    assert params['reduceOnly'] is True

@pytest.mark.asyncio
async def test_mode_flip_on_10001(adapter, mock_ccxt_bybit):
    adapter._position_mode = 'MergedSingle'
    mock_ccxt_bybit.create_order.side_effect = [
        Exception("bybit 10001 Side invalid"),
        {'id': 'order_recovered'}
    ]
    res = await adapter.create_order('BTC/USDT', 'market', 'BUY', 0.001)
    assert res['id'] == 'order_recovered'
    assert adapter._position_mode == 'BothSide'
    assert mock_ccxt_bybit.create_order.call_count == 2
    params = get_params(mock_ccxt_bybit.create_order)
    assert params['positionIdx'] == 1

@pytest.mark.asyncio
async def test_set_position_sl_tp_already_passed(adapter, mock_ccxt_bybit):
    # Simulate "Mark Price higher than base price" 10001 error
    # The adapter looks for "higher than", "lower than", or "base_price"
    mock_ccxt_bybit.privatePostV5PositionTradingStop.return_value = \
        {'retCode': 10001, 'retMsg': 'already passed'}
    res = await adapter.set_position_sl_tp('BTC/USDT', 'BUY', sl=50000, tp=60000)
    
    # Should treat as set to avoid loops
    assert res is not None
    assert res.get('sl_set') is True or res.get('tp_set') is True
    assert mock_ccxt_bybit.privatePostV5PositionTradingStop.call_count == 1
