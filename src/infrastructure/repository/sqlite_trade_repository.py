import json
from typing import List, Optional, Dict, Any
from src.domain.repository import ITradeRepository
from src.domain.models import Trade
from .database import DataManager

class SQLiteTradeRepository(ITradeRepository):
    """
    SQLite implementation of Trade Repository using DataManager.
    This acts as a bridge during migration from DataManager to Clean Architecture.
    """
    def __init__(self, data_manager: DataManager):
        self.dm = data_manager

    async def save_trade(self, trade: Trade) -> int:
        # Convert Pydantic model back to dict for legacy DataManager compat
        trade_dict = trade.model_dump()
        # DataManager expects some fields renamed or formatted
        if 'meta' in trade_dict:
            # DataManager calls save_position which handles meta_json
            pass
        
        # Mapping model fields to DataManager/DB expected fields
        # Note: Position and Trade are slightly different in DataManager.save_position
        return await self.dm.save_position(trade_dict)

    async def get_active_positions(self, profile_id: int) -> List[Trade]:
        rows = await self.dm.get_active_positions(profile_id)
        return [Trade(**r) for r in rows]

    async def get_active_positions_on_exchange(self, exchange_name: str) -> List[Trade]:
        rows = await self.dm.get_active_positions_on_exchange(exchange_name)
        return [Trade(**r) for r in rows]

    async def get_trade_history(self, profile_id: int, limit: int = 100) -> List[Trade]:
        rows = await self.dm.get_trade_history(profile_id, limit)
        return [Trade(**r) for r in rows]

    async def update_status(self, trade_id: int, status: str, **kwargs) -> None:
        exit_price = kwargs.get('exit_price')
        pnl = kwargs.get('pnl')
        exit_reason = kwargs.get('exit_reason')
        await self.dm.update_position_status(trade_id, status, exit_price, pnl, exit_reason)
