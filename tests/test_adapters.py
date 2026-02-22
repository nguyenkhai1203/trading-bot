import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from adapters.binance_adapter import BinanceAdapter
from adapters.bybit_adapter import BybitAdapter

class TestBinanceAdapter:
    @pytest.fixture
    def mock_ccxt_binance(self):
        exch = MagicMock()
        exch.id = 'binance'
        exch.options = {}
        exch.fetch_balance = AsyncMock(return_value={})
        exch.fetch_open_orders = AsyncMock(return_value=[{'id': 'std_1', 'symbol': 'BTC/USDT'}])
        exch.fapiPrivateGetOpenAlgoOrders = AsyncMock(return_value=[{'algoId': 'algo_1', 'symbol': 'BTCUSDT', 'algoType': 'STOP_LOSS'}])
        exch.cancel_order = AsyncMock(return_value={'id': 'cancelled'})
        exch.fapiPrivateDeleteAlgoOrder = AsyncMock(return_value={'id': 'algo_cancelled'})
        exch.create_order = AsyncMock(return_value={'id': 'new_order'})
        exch.amount_to_precision = MagicMock(side_effect=lambda s, q: str(q))
        return exch

    @pytest.fixture
    def adapter(self, mock_ccxt_binance):
        with patch('adapters.binance_adapter.BINANCE_API_KEY', 'test_key'), \
             patch('adapters.binance_adapter.BINANCE_API_SECRET', 'test_secret'):
            return BinanceAdapter(mock_ccxt_binance)

    @pytest.mark.asyncio
    async def test_fetch_open_orders_merge(self, adapter, mock_ccxt_binance):
        """Verify that standard and algo orders are merged."""
        orders = await adapter.fetch_open_orders('BTC/USDT:USDT')
        
        assert len(orders) == 2
        assert any(o.get('id') == 'std_1' for o in orders)
        assert any(o.get('algoId') == 'algo_1' for o in orders)
        assert any(o.get('is_algo') is True for o in orders)

    @pytest.mark.asyncio
    async def test_cancel_order_retry_fallback(self, adapter, mock_ccxt_binance):
        """Verify that cancel_order retries as ALGO if standard fails with 'not found'."""
        # Standard cancel fails with "not found"
        mock_ccxt_binance.cancel_order.side_effect = [
            Exception("Order not found"), 
            {'id': 'std_retry_success'}
        ]
        
        # We need to mock the is_algo logic. The adapter checks params or previous runs.
        # If we call it with default, it tries standard first.
        res = await adapter.cancel_order('order_123', 'BTC/USDT')
        
        # Should have called cancel_order twice (standard then retry which happens to be same method in CCXT but different params/logic in adapter)
        assert mock_ccxt_binance.cancel_order.call_count == 2
        # In the adapter, it catches Exception and retries.

    @pytest.mark.asyncio
    async def test_set_leverage_signed_post(self, adapter):
        """Verify that set_leverage uses the custom signed post."""
        with patch('adapters.binance_adapter.requests.post') as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = {'code': 200, 'symbol': 'BTCUSDT', 'leverage': 20}
            
            await adapter.set_leverage('BTC/USDT:USDT', 20)
            
            assert mock_post.called
            args, kwargs = mock_post.call_args
            assert 'fapi.binance.com/fapi/v1/leverage' in args[0]
            assert 'leverage=20' in args[0]

    @pytest.mark.asyncio
    async def test_place_stop_orders(self, adapter, mock_ccxt_binance):
        """Verify placement of SL and TP orders."""
        res = await adapter.place_stop_orders('BTC/USDT', 'BUY', 1.0, sl=40000.0, tp=60000.0)
        
        assert res['sl_id'] == 'new_order'
        assert res['tp_id'] == 'new_order'
        assert mock_ccxt_binance.create_order.call_count == 2
        
        # Check SL call
        sl_call = mock_ccxt_binance.create_order.call_args_list[0]
        # create_order(symbol, type, side, amount, price, params)
        assert sl_call[0][1] == 'STOP_MARKET'
        assert sl_call[1]['params']['stopPrice'] == 40000.0

class TestBybitAdapter:
    @pytest.fixture
    def mock_ccxt_bybit(self):
        exch = MagicMock()
        exch.id = 'bybit'
        exch.options = {}
        exch.fetch_ohlcv = AsyncMock(return_value=[])
        exch.fetch_positions = AsyncMock(return_value=[
            {'symbol': 'LTC/USDT:USDT', 'contracts': 10.0, 'info': {'category': 'linear', 'size': '10'}}
        ])
        exch.cancel_order = AsyncMock(return_value={'id': 'cancelled'})
        exch.create_order = AsyncMock(return_value={'id': 'new_order'})
        exch.privatePostV5PositionTradingStop = AsyncMock(return_value={'retCode': 0})
        exch.market = MagicMock(return_value={'id': 'LTCUSDT'})
        return exch

    @pytest.fixture
    def adapter(self, mock_ccxt_bybit):
        adapter = BybitAdapter(mock_ccxt_bybit)
        adapter._normalize_symbol = MagicMock(return_value='LTCUSDT')
        return adapter

    @pytest.mark.asyncio
    async def test_fetch_ohlcv_mapping(self, adapter, mock_ccxt_bybit):
        """Verify timeframe mapping (8h -> 4h)."""
        await adapter.fetch_ohlcv('BTC/USDT', '8h')
        args, kwargs = mock_ccxt_bybit.fetch_ohlcv.call_args
        assert args[1] == '4h'

    @pytest.mark.asyncio
    async def test_fetch_positions_filtering(self, adapter, mock_ccxt_bybit):
        """Verify that positions are filtered for linear category."""
        mock_ccxt_bybit.fetch_positions.return_value = [
            {'symbol': 'BTC/USDT', 'contracts': 1.0, 'info': {'category': 'spot'}},
            {'symbol': 'LTC/USDT:USDT', 'contracts': 10.0, 'info': {'category': 'linear'}}
        ]
        
        positions = await adapter.fetch_positions()
        assert len(positions) == 1
        assert positions[0]['info']['category'] == 'linear'

    @pytest.mark.asyncio
    async def test_cancel_order_trigger_fallback(self, adapter, mock_ccxt_bybit):
        """Verify fallback to trigger=True for Bybit."""
        mock_ccxt_bybit.cancel_order.side_effect = [
            Exception("Order not found"),
            {'id': 'retry_success'}
        ]
        
        await adapter.cancel_order('order_1', 'BTC/USDT')
        assert mock_ccxt_bybit.cancel_order.call_count == 2
        # In adapter, kond_params is passed as 3rd positional arg
        assert mock_ccxt_bybit.cancel_order.call_args[0][2]['trigger'] is True

    @pytest.mark.asyncio
    async def test_set_position_sl_tp(self, adapter, mock_ccxt_bybit):
        """Verify use of privatePostV5PositionTradingStop."""
        res = await adapter.set_position_sl_tp('LTC/USDT:USDT', 'BUY', sl=100.0)
        
        assert res['sl_set'] is True
        assert mock_ccxt_bybit.privatePostV5PositionTradingStop.called
        body = mock_ccxt_bybit.privatePostV5PositionTradingStop.call_args[0][0]
        assert body['stopLoss'] == '100.0'
        assert body['symbol'] == 'LTCUSDT'
