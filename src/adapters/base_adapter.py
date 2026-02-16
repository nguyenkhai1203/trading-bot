from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any

class BaseAdapter(ABC):
    """
    Abstract Base Class for Exchange Adapters.
    Standardizes interaction with different exchanges (Binance, Bybit, etc.).
    """

    def __init__(self, exchange_client):
        self.exchange = exchange_client
        self.name = exchange_client.id.upper() if exchange_client else "UNKNOWN"

    @property
    def is_authenticated(self) -> bool:
        """Check if the exchange client has legitimate trading permissions."""
        # Priority: Check explicit capabilities if set (from BaseExchangeClient)
        if hasattr(self, 'permissions'):
            return self.permissions.get('can_trade', False)
            
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
    async def fetch_positions(self) -> List[Dict]:
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

    async def close(self):
        """Close the underlying exchange connection."""
        if self.exchange and hasattr(self.exchange, 'close'):
            await self.exchange.close()
