import ccxt.async_support as ccxt
import asyncio
import logging
from typing import Dict, List, Optional, Any
from .base_adapter import BaseAdapter
from base_exchange_client import BaseExchangeClient
from config import BYBIT_API_KEY, BYBIT_API_SECRET

class BybitAdapter(BaseExchangeClient, BaseAdapter):
    """
    Bybit Adapter implementation using CCXT.
    Focuses on USDT Perpetual Futures (Linear).
    BaseExchangeClient provides time synchronization and retry logic.
    """

    def __init__(self, exchange_client=None):
        """
        Initialize Bybit adapter.
        If exchange_client is provided, use it. Otherwise create new ccxt.bybit instance.
        """
        # Initialize BaseAdapter (wrapper)
        BaseAdapter.__init__(self, exchange_client)
        self.name = 'BYBIT'

        client = exchange_client
        if not client:
            # Initialize CCXT Bybit instance
            options = {
                'defaultType': 'swap',  # USDT Perpetual
                'adjustForTimeDifference': True,
                'recvWindow': 10000,
            }
            client = ccxt.bybit({
                'apiKey': BYBIT_API_KEY,
                'secret': BYBIT_API_SECRET,
                'options': options,
                'enableRateLimit': True,
            })
            # Update BaseAdapter's exchange ref
            self.exchange = client
        
        # Initialize BaseExchangeClient (functionality)
        BaseExchangeClient.__init__(self, client)
        
        self.logger = logging.getLogger(__name__)

    def __getattr__(self, name):
        """Proxy unknown attributes to the underlying exchange object (ccxt)."""
        return getattr(self.exchange, name)

    async def sync_time(self) -> bool:
        """Sync time and load markets."""
        try:
            # BaseExchangeClient.sync_server_time handles the heavy lifting
            await self.sync_server_time()
            await self.exchange.load_markets()
            return True
        except Exception as e:
            self.logger.error(f"[Bybit] Sync time/markets failed: {e}")
            return False

    async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 100) -> List[list]:
        """Fetch OHLCV klines with Bybit-specific mapping."""
        # Bybit doesn't support '8h'. Map to something else or skip.
        # Bybit V5 supports: 1,3,5,15,30,60,120,240,360,720,D,M,W
        mapping = {
            '8h': '4h', # closest supported
        }
        target_tf = mapping.get(timeframe, timeframe)
        
        try:
            return await self.exchange.fetch_ohlcv(symbol, target_tf, limit=limit)
        except Exception as e:
            self.logger.error(f"[Bybit] Fetch OHLCV failed for {symbol} ({target_tf}): {e}")
            return []

    async def fetch_ticker(self, symbol: str) -> Dict:
        """Fetch current ticker data."""
        return await self.exchange.fetch_ticker(symbol)

    async def fetch_tickers(self, symbols: List[str]) -> Dict:
        """Fetch multiple tickers."""
        try:
            # Bybit supports fetching all or specific
            return await self.exchange.fetch_tickers(symbols)
        except Exception as e:
            self.logger.error(f"[Bybit] Fetch tickers failed: {e}")
            return {}

    async def fetch_open_orders(self, symbol: Optional[str] = None) -> List[Dict]:
        """Fetch open orders (all or specific symbol)."""
        try:
            # Bybit Unified Margin often requires symbol for open orders, 
            # but CCXT handles pagination for 'all' if symbol is None
            return await self.exchange.fetch_open_orders(symbol)
        except Exception as e:
            self.logger.error(f"[Bybit] Fetch open orders failed: {e}")
            return []

    async def fetch_positions(self) -> List[Dict]:
        """
        Fetch active positions.
        Normalizes Bybit response to standard CCXT structure.
        """
        try:
            # CCXT fetch_positions usually returns list of dicts
            positions = await self.exchange.fetch_positions()
            # CCXT usually normalizes this well, but we ensure 'contracts' > 0
            active_positions = [p for p in positions if float(p.get('contracts', 0) or p.get('info', {}).get('size', 0)) > 0]
            return active_positions
        except Exception as e:
            self.logger.error(f"[Bybit] Fetch positions failed: {e}")
            return []

    async def create_order(self, symbol: str, type: str, side: str, amount: float, price: Optional[float] = None, params: Dict = {}) -> Dict:
        """Create a new order."""
        try:
            return await self.exchange.create_order(symbol, type, side, amount, price, params)
        except Exception as e:
            self.logger.error(f"[Bybit] Create order failed for {symbol}: {e}")
            raise e

    async def cancel_order(self, order_id: str, symbol: str, params: Dict = {}) -> Dict:
        """Cancel an order."""
        try:
            # Merge Bybit-specific params for linear futures
            extra_params = {'category': 'linear'}
            extra_params.update(params)
            return await self.exchange.cancel_order(order_id, symbol, extra_params)
        except Exception as e:
            self.logger.error(f"[Bybit] Cancel order failed for {symbol}: {e}")
            raise e

    async def set_leverage(self, symbol: str, leverage: int, params: Dict = {}):
        """Set leverage for a symbol (V5 linear)."""
        try:
            # Resolve to native Bybit ID (e.g. LTCUSDT) for V5 API compatibility
            # CCXT bybit set_leverage is strict about symbol format
            try:
                market = self.exchange.market(symbol)
                native_symbol = market.get('id', symbol)
            except:
                native_symbol = symbol.replace('/', '').replace(':USDT', '')

            # Merge with passed params if any
            extra = {'category': 'linear'}
            extra.update(params)
            
            # CCXT Bybit V5 set_leverage(leverage, symbol, params)
            self.logger.debug(f"[Bybit] Calling CCXT set_leverage({leverage}, {native_symbol}, {extra})")
            await self.exchange.set_leverage(leverage, native_symbol, params=extra)
        except Exception as e:
            # Bybit throws error if leverage is already set to that value
            if "not modified" not in str(e).lower() and "already" not in str(e).lower():
                self.logger.warning(f"[Bybit] Set leverage failed for {symbol}: {e}")

    async def fetch_balance(self) -> Dict:
        """Fetch balance for UNIFIED account (V5 linear)."""
        try:
            # Bybit V5 often needs accountType=UNIFIED specified
            res = await self.exchange.fetch_balance()
            
            # Diagnostic Log: See exactly what CCXT found
            total_usdt = res.get('total', {}).get('USDT', 0)
            free_usdt = res.get('free', {}).get('USDT', 0)
            self.logger.debug(f"[Bybit] Balance Fetched | Total USDT: {total_usdt} | Free USDT: {free_usdt}")
            
            return res
        except Exception as e:
            self.logger.error(f"[Bybit] Fetch balance failed: {e}")
            return {}

    async def set_margin_mode(self, symbol: str, mode: str, params: Dict = {}):
        """Set margin mode (ISOLATED/CROSS)."""
        # Resolve to native Bybit ID (e.g. LTCUSDT)
        try:
            market = self.exchange.market(symbol)
            native_symbol = market.get('id', symbol)
        except:
            native_symbol = symbol.replace('/', '').replace(':USDT', '')

        # Try both lowercase and uppercase as Bybit V5 can be picky based on account type
        modes_to_try = [mode.lower(), mode.upper()]
        last_err = None
        
        for m in modes_to_try:
            try:
                extra = {'category': 'linear'}
                extra.update(params)
                # Bybit V5 set_margin_mode(margin_mode, symbol, params)
                self.logger.debug(f"[Bybit] Calling CCXT set_margin_mode({m}, {native_symbol}, {extra})")
                await self.exchange.set_margin_mode(m, native_symbol, params=extra)
                return # Success
            except Exception as e:
                last_err = e
                err_str = str(e).lower()
                if "not modified" in err_str or "already" in err_str:
                    return # Already set
                continue # Try next casing
        
        self.logger.warning(f"[Bybit] Set margin mode failed for {symbol}: {last_err}")

    async def fetch_order(self, order_id: str, symbol: str, params: Dict = {}) -> Dict:
        """Fetch order with Bybit-specific acknowledgment to hide warnings."""
        extra_params = {'acknowledged': True}
        extra_params.update(params)
        return await self._execute_with_timestamp_retry(
            self.exchange.fetch_order,
            order_id,
            symbol,
            extra_params
        )

    async def close(self):
        """Close exchange connection."""
        await self.exchange.close()
