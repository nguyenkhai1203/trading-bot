from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any

class BaseAdapter(ABC):
    """
    Abstract Base Class for Exchange Adapters.
    Standardizes interaction with different exchanges (Binance, Bybit, etc.).
    """

    def __init__(self, exchange_client):
        self.exchange = exchange_client
        # Standardize: binanceusdm -> BINANCE, bybit -> BYBIT
        raw_id = exchange_client.id.upper() if exchange_client else "UNKNOWN"
        if 'BINANCE' in raw_id:
            self.name = 'BINANCE'
        elif 'BYBIT' in raw_id:
            self.name = 'BYBIT'
        else:
            self.name = raw_id

    @property
    def is_authenticated(self) -> bool:
        """Check if the exchange client has legitimate trading permissions."""
        # Priority: Check explicit capabilities if set (from BaseExchangeClient)
        if hasattr(self.exchange, 'permissions'):
            return self.exchange.permissions['can_trade']
            
        # Fallback: Check CCXT credentials directly (Legacy)
        if not self.exchange:
            return False
        api_key = getattr(self.exchange, 'apiKey', None)
        return api_key is not None and len(str(api_key)) > 10 and 'your_' not in str(api_key)

    @abstractmethod
    async def sync_time(self) -> bool:
        """Sync time with exchange server."""
        pass

    @abstractmethod
    async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 100) -> List[list]:
        """Fetch OHLCV klines."""
        pass

    @abstractmethod
    async def fetch_ticker(self, symbol: str) -> Dict:
        """Fetch current ticker data."""
        pass

    @abstractmethod
    async def fetch_tickers(self, symbols: List[str]) -> Dict:
        """Fetch multiple tickers."""
        pass

    @abstractmethod
    async def fetch_open_orders(self, symbol: Optional[str] = None) -> List[Dict]:
        """Fetch open orders (all or specific symbol)."""
        pass

    @abstractmethod
    async def fetch_positions(self, params: Dict = {}) -> List[Dict]:
        """Fetch active positions."""
        pass

    @abstractmethod
    async def create_order(self, symbol: str, type: str, side: str, amount: float, price: Optional[float] = None, params: Dict = {}) -> Dict:
        """Create a new order."""
        pass

    @abstractmethod
    async def cancel_order(self, order_id: str, symbol: str, params: Dict = {}) -> Dict:
        """Cancel an order."""
        pass

    @abstractmethod
    async def set_leverage(self, symbol: str, leverage: int, params: Dict = {}):
        """Set leverage for a symbol."""
        pass
        
    @abstractmethod
    async def set_margin_mode(self, symbol: str, mode: str, params: Dict = {}):
        """Set margin mode (ISOLATED/CROSS)."""
        pass

    @abstractmethod
    async def fetch_balance(self) -> Dict:
        """Fetch account balance."""
        pass

    @abstractmethod
    async def cancel_all_orders(self, symbol: str):
        """Cancel ALL orders for a symbol (standard + algo/conditional)."""
        pass

    @abstractmethod
    async def fetch_order(self, order_id: str, symbol: str, params: Dict = {}) -> Dict:
        """Fetch a single order, with fallback to conditional queue if needed."""
        pass

    @abstractmethod
    async def place_stop_orders(
        self, symbol: str, side: str, qty: float,
        sl: Optional[float] = None, tp: Optional[float] = None
    ) -> Dict:
        """
        Place SL and/or TP orders for an open position.
        Returns dict with keys: sl_id, tp_id (None if not placed).

        Encapsulates all exchange-specific differences:
          - Binance: STOP_MARKET / TAKE_PROFIT_MARKET order types
          - Bybit:   market + triggerDirection + category: linear
        """
        pass

    @abstractmethod
    async def cancel_stop_orders(
        self, symbol: str,
        sl_id: Optional[str] = None,
        tp_id: Optional[str] = None
    ):
        """
        Cancel existing SL and/or TP orders by order ID.

        Encapsulates all exchange-specific differences:
          - Binance: algo order cancel (fapiPrivateDeleteAlgoOrder or is_algo=True flag)
          - Bybit:   conditional order cancel with category:linear + trigger fallback
        """
        pass

    @abstractmethod
    async def close_position(self, symbol: str, side: str, qty: float) -> Dict:
        """
        Market-close an open position.
        Cancels all existing stop orders first, then sends market reduceOnly order.

        Encapsulates all exchange-specific differences:
          - Binance: cancel_all + cancel algo orders + MARKET reduceOnly
          - Bybit:   cancel_all (category:linear) + market reduceOnly + category:linear
        """
        pass

    @abstractmethod
    def round_qty(self, symbol: str, qty: float) -> float:
        """Round quantity to exchange-specific precision."""
        pass

    @abstractmethod
    def is_spot(self, symbol: str) -> bool:
        """Check if a symbol is a spot symbol."""
        pass

    async def close(self):
        """Close the underlying exchange connection."""
        if self.exchange and hasattr(self.exchange, 'close'):
            await self.exchange.close()
