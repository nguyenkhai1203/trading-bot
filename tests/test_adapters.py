import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from src.infrastructure.adapters.binance_adapter import BinanceAdapter
from src.infrastructure.adapters.bybit_adapter import BybitAdapter

@pytest.fixture(autouse=True)
def isolate_env():
    """Ensure tests aren't affected by local .env."""
    with patch('os.getenv', return_value=None):
        yield

class TestBinanceAdapter:
    @pytest.fixture
    def mock_ccxt_binance(self):
        exch = AsyncMock() # Use AsyncMock for the whole exchange to be safe
        exch.id = 'binance'
        exch.options = {}
        # Explicitly set some as AsyncMock if needed, but AsyncMock(exch) handles it
        exch.fetch_balance = AsyncMock(return_value={})
        exch.fetch_open_orders = AsyncMock(return_value=[{'id': 'std_1', 'symbol': 'BTC/USDT'}])
        exch.fapiPrivateGetOpenAlgoOrders = AsyncMock(return_value=[{'algoId': 'algo_1', 'symbol': 'BTCUSDT', 'algoType': 'STOP_LOSS'}])
        exch.cancel_order = AsyncMock(return_value={'id': 'cancelled'})
        exch.fapiPrivateDeleteAlgoOrder = AsyncMock(return_value={'id': 'algo_cancelled'})
        exch.fapiPrivateDeleteAlgoOpenOrders = AsyncMock(return_value={'id': 'algo_all_cancelled'})
        exch.create_order = AsyncMock(return_value={'id': 'new_order'})
        exch.amount_to_precision = MagicMock(side_effect=lambda s, q: str(q))
        exch.fetch_time = AsyncMock(return_value=1000)
        exch.load_time_difference = AsyncMock(return_value=0)
        return exch

    @pytest.fixture
    def adapter(self, mock_ccxt_binance):
        with patch('src.infrastructure.adapters.binance_adapter.BINANCE_API_KEY', 'test_key'), \
             patch('src.infrastructure.adapters.binance_adapter.BINANCE_API_SECRET', 'test_secret'):
            # Pass mock_ccxt_binance to constructor
            return BinanceAdapter(mock_ccxt_binance)

    @pytest.mark.asyncio
    async def test_fetch_open_orders_merge(self, adapter, mock_ccxt_binance):
        """Verify that standard and algo orders are merged."""
        orders = await adapter.fetch_open_orders('BTC/USDT:USDT')
        
        assert len(orders) == 2
        assert any(o.get('id') == 'std_1' for o in orders)
        assert any(o.get('id') == 'algo_1' or o.get('algoId') == 'algo_1' for o in orders)
        assert any(o.get('is_algo') is True for o in orders)

    @pytest.mark.asyncio
    async def test_cancel_order_error_handling(self, adapter, mock_ccxt_binance):
        """Verify that cancel_order handles errors gracefully."""
        mock_ccxt_binance.cancel_order.side_effect = Exception("Order not found")
        
        with pytest.raises(Exception):
            await adapter.cancel_order('order_123', 'BTC/USDT')

    @pytest.mark.asyncio
    async def test_place_stop_orders(self, adapter, mock_ccxt_binance):
        """Verify placement of SL and TP orders."""
        res = await adapter.place_stop_orders('BTC/USDT', 'BUY', 1.0, sl=40000.0, tp=60000.0)
        
        assert res['sl_id'] == 'new_order'
        assert res['tp_id'] == 'new_order'
        assert mock_ccxt_binance.create_order.call_count == 2
        
        # Check SL call
        args, kwargs = mock_ccxt_binance.create_order.call_args_list[0]
        assert args[1] == 'STOP_MARKET'
        params = kwargs.get('params') or (args[5] if len(args) > 5 else {})
        assert params.get('stopPrice') == 40000.0

class TestBybitAdapter:
    @pytest.fixture
    def mock_ccxt_bybit(self):
        exch = AsyncMock()
        exch.id = 'bybit'
        exch.options = {}
        exch.fetch_ohlcv = AsyncMock(return_value=[])
        exch.fetch_ticker = AsyncMock(return_value={'markPrice': '50000', 'info': {'markPrice': '50000'}})
        exch.fetch_position_mode = AsyncMock(return_value={'mode': 'hedged', 'hedged': True})
        exch.privateGetV5PositionList = AsyncMock(return_value={'result': {'list': [{'symbol': 'LTC/USDT:USDT', 'size': '10', 'avgPrice': '100', 'category': 'linear'}]}})
        exch.cancel_order = AsyncMock(return_value={'id': 'cancelled'})
        exch.create_order = AsyncMock(return_value={'id': 'new_order'})
        exch.cancel_all_orders = AsyncMock(return_value={'id': 'all_cancelled'})
        exch.market = MagicMock(return_value={'id': 'LTCUSDT', 'symbol': 'LTC/USDT:USDT', 'limits': {'cost': {'min': 1.0}, 'amount': {'min': 0.1}}})
        exch.amount_to_precision = MagicMock(side_effect=lambda s, q: str(q))
        exch.price_to_precision = MagicMock(side_effect=lambda s, p: str(p))
        exch.fetch_time = AsyncMock(return_value=1000)
        exch.load_time_difference = AsyncMock(return_value=0)
        return exch

    @pytest.fixture
    def adapter(self, mock_ccxt_bybit):
        return BybitAdapter(mock_ccxt_bybit)

    @pytest.mark.asyncio
    async def test_fetch_ohlcv_mapping(self, adapter, mock_ccxt_bybit):
        """Verify timeframe mapping (8h -> 4h)."""
        await adapter.fetch_ohlcv('BTC/USDT', '8h')
        args, kwargs = mock_ccxt_bybit.fetch_ohlcv.call_args
        assert args[1] == '4h'

    @pytest.mark.asyncio
    async def test_fetch_positions_filtering(self, adapter, mock_ccxt_bybit):
        """Verify that positions are filtered and mapped correctly."""
        positions = await adapter.fetch_positions()
        assert len(positions) == 1
        assert positions[0]['contracts'] == 10.0
        assert positions[0]['side'] == '' # side not in our mock list but we can fix mock
        
    @pytest.mark.asyncio
    async def test_place_stop_orders(self, adapter, mock_ccxt_bybit):
        """Verify Bybit SL/TP placement via market orders."""
        res = await adapter.place_stop_orders('LTC/USDT:USDT', 'BUY', 10.0, sl=90.0)
        assert res['sl_id'] == 'new_order'
        assert mock_ccxt_bybit.create_order.called
        # Check params for triggerDirection
        args, kwargs = mock_ccxt_bybit.create_order.call_args
        params = kwargs.get('params') or (args[5] if len(args) > 5 else {})
        assert float(params.get('stopPrice')) == 90.0
        assert params.get('triggerDirection') == 'descending'

    @pytest.mark.asyncio
    async def test_check_min_notional(self, adapter):
        valid, reason, qty = adapter.check_min_notional('LTC/USDT:USDT', 100.0, 0.5)
        assert valid is True
        
        valid_low, reason_low, qty_low = adapter.check_min_notional('LTC/USDT:USDT', 1.0, 0.5)
        assert valid_low is False # 0.5 * 1.0 < 1.0
