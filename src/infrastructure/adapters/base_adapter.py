from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any
import logging

class BaseAdapter(ABC):
    """
    Abstract Base Class for Exchange Adapters.
    Standardizes interaction with different exchanges (Binance, Bybit, etc.).
    """

    def __init__(self, exchange_client, dry_run: bool = True):
        self.exchange = exchange_client
        self.dry_run = dry_run
        self.logger = logging.getLogger(self.__class__.__name__)
        
        # Standardize: binanceusdm -> BINANCE, bybit -> BYBIT
        raw_id = exchange_client.id.upper() if exchange_client else "UNKNOWN"
        if 'BINANCE' in raw_id:
            self.name = 'BINANCE'
        elif 'BYBIT' in raw_id:
            self.name = 'BYBIT'
        else:
            self.name = raw_id
            
        self._server_time_offset = 0
        self.can_trade = not dry_run
        self.can_view_balance = True

    def set_permissions(self, can_trade: bool = False, can_view_balance: bool = True):
        """Set adapter capabilities based on API key permissions."""
        self.can_trade = can_trade
        self.can_view_balance = can_view_balance
        self.can_use_private = can_trade or can_view_balance

    @property
    def is_public_only(self) -> bool:
        """Check if the adapter is limited to public data only."""
        return not self.can_trade and not self.can_view_balance

    def normalize_status(self, raw_status: str) -> str:
        """Standardize exchange status to internal DB status."""
        if not raw_status: return 'OPENED'
        s = raw_status.lower()
        if s in ['open', 'untouched', 'new', 'partially_filled', 'working']:
            return 'OPENED'
        if s in ['closed', 'filled']:
            return 'CLOSED'
        if s in ['canceled', 'cancelled', 'expired', 'rejected']:
            return 'CANCELLED'
        return 'OPENED'

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
    async def fetch_balance(self) -> Dict:
        """Fetch account balance."""
        pass

    @abstractmethod
    async def place_stop_orders(
        self, symbol: str, side: str, qty: float,
        sl: Optional[float] = None, tp: Optional[float] = None
    ) -> Dict:
        """Place SL and/or TP orders."""
        pass

    @abstractmethod
    async def cancel_stop_orders(self, symbol: str, sl_id: Optional[str] = None, tp_id: Optional[str] = None):
        """Cancel existing SL and/or TP orders."""
        pass

    @abstractmethod
    async def close_position(self, symbol: str, side: str, qty: float) -> Dict:
        """Close an open position."""
        pass

    @abstractmethod
    def round_qty(self, symbol: str, qty: float) -> float:
        """Round quantity to exchange precision."""
        pass

    @abstractmethod
    def is_spot(self, symbol: str) -> bool:
        """Check if symbol is spot."""
        pass

    @abstractmethod
    async def ensure_isolated_and_leverage(self, symbol: str, leverage: int):
        """Ensure margin mode is isolated and leverage set for target symbol."""
        pass

    @abstractmethod
    def check_min_notional(self, symbol: str, price: float, qty: float) -> tuple[bool, str, float]:
        """Verify if order meets exchange min notional. Returns (is_valid, reason, correct_qty)."""
        pass

    @abstractmethod
    def get_unified_symbol(self, symbol: str) -> str:
        """Map a native or partial symbol back to its unified format."""
        pass

    @abstractmethod
    def is_tpsl_attached_supported(self) -> bool:
        """Returns True if the exchange supports attaching SL/TP to the main order."""
        pass

    @abstractmethod
    def infer_exit_reason(self, close_trade: Dict, pos_data: Dict) -> str:
        """Determine SL or TP from exchange trade data."""
        pass

    async def close(self):
        """Close the connection."""
        if self.exchange and hasattr(self.exchange, 'close'):
            await self.exchange.close()
