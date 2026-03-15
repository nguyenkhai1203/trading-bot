from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any
from src.domain.models import Trade, Position

class ITradeRepository(ABC):
    @abstractmethod
    async def save_trade(self, trade: Trade) -> int:
        """Insert or Update a trade/position."""
        pass

    @abstractmethod
    async def get_active_positions(self, profile_id: int) -> List[Trade]:
        """Fetch all ACTIVE or OPENED positions for a specific profile."""
        pass

    @abstractmethod
    async def get_trade_by_order_id(self, order_id: str) -> Optional[Trade]:
        """Fetch a trade by its exchange_order_id or client_order_id."""
        pass


    @abstractmethod
    async def get_trade_history(self, profile_id: int, limit: int = 100) -> List[Trade]:
        """Fetch closed/cancelled trade history for a profile."""
        pass

    @abstractmethod
    async def update_status(self, trade_id: int, status: str, **kwargs) -> None:
        """Atomically update trade status and details."""
        pass

    @abstractmethod
    async def get_all_active_trade_profile_ids(self) -> List[int]:
        """Fetch all profile IDs that currently have non-closed trades."""
        pass

    @abstractmethod
    async def get_profile_by_id(self, profile_id: int) -> Optional[Dict[str, Any]]:
        """Fetch profile metadata directly."""
        pass

class IProfileRepository(ABC):
    @abstractmethod
    async def get_active_profiles(self) -> List[Dict[str, Any]]:
        """Get all active trading profiles."""
        pass

    @abstractmethod
    async def get_profile_by_id(self, profile_id: int) -> Optional[Dict[str, Any]]:
        """Get profile details by ID."""
        pass

class ISentimentRepository(ABC):
    @abstractmethod
    async def upsert_sentiment(self, symbol: str, data: Dict[str, Any]) -> None:
        """Save latest market sentiment."""
        pass

    @abstractmethod
    async def get_latest_sentiment(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get latest market sentiment."""
        pass
