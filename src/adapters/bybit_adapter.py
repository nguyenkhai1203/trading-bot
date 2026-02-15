import ccxt.async_support as ccxt
import asyncio
import logging
from typing import Dict, List, Optional, Any
from .base_adapter import BaseAdapter
from config import BYBIT_API_KEY, BYBIT_API_SECRET

class BybitAdapter(BaseAdapter):
    """
    Bybit Adapter implementation using CCXT.
    Focuses on USDT Perpetual Futures (Linear).
    """

    def __init__(self, exchange_client=None):
        """
        Initialize Bybit adapter.
        If exchange_client is provided, use it. Otherwise create new ccxt.bybit instance.
        """
        if exchange_client:
            super().__init__(exchange_client)
        else:
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
            super().__init__(client)
        
        self.logger = logging.getLogger(__name__)

    async def sync_time(self) -> bool:
        """Sync time and load markets."""
        try:
            await self.exchange.load_markets()
            # self.exchange.msg(f"Market data loaded: {len(self.exchange.markets)} symbols")
            return True
        except Exception as e:
            self.logger.error(f"[Bybit] Sync time failed: {e}")
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
            return await self.exchange.cancel_order(order_id, symbol, params)
        except Exception as e:
            self.logger.error(f"[Bybit] Cancel order failed for {symbol}: {e}")
            raise e

    async def set_leverage(self, symbol: str, leverage: int):
        """Set leverage for a symbol."""
        try:
            await self.exchange.set_leverage(leverage, symbol)
        except Exception as e:
            # Bybit throws error if leverage is already set to that value, usually safe to ignore or log warning
            if "not modified" not in str(e).lower():
                self.logger.warning(f"[Bybit] Set leverage failed for {symbol}: {e}")

    async def set_margin_mode(self, symbol: str, mode: str):
        """Set margin mode (ISOLATED/CROSS)."""
        try:
            # 'ISOLATED' or 'CROSS'
            await self.exchange.set_margin_mode(mode.upper(), symbol)
        except Exception as e:
             if "not modified" not in str(e).lower():
                self.logger.warning(f"[Bybit] Set margin mode failed for {symbol}: {e}")

    async def close(self):
        """Close exchange connection."""
        await self.exchange.close()
    
    def __getattr__(self, name):
        """Proxy unknown attributes to the underlying exchange object (ccxt)."""
        return getattr(self.exchange, name)
