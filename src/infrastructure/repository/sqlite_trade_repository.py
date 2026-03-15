import json
from typing import List, Optional, Dict, Any
from src.domain.repository import ITradeRepository
from src.domain.models import Trade
from .database import DataManager
import aiosqlite

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
        # Explicit mapping: Pydantic exports 'meta' (dict), DataManager expects 'meta' (dict) which it serializes to 'meta_json'
        # Ensure status is carried through correctly (not overridden by DataManager default 'OPENED')
        if 'meta' not in trade_dict or trade_dict.get('meta') is None:
            trade_dict['meta'] = {}
        # DataManager calls save_position which handles meta -> meta_json serialization
        return await self.dm.save_position(trade_dict)

    def _map_row_to_trade(self, row: dict) -> Trade:
        """Helper to properly hydrate Pydantic Trade model from SQLite row dictionary."""
        # SQLite stores meta as JSON string in 'meta_json' column
        meta_json = row.pop('meta_json', None)
        if meta_json:
            try:
                row['meta'] = json.loads(meta_json)
            except Exception:
                row['meta'] = {}
        elif 'meta' not in row:
            row['meta'] = {}
            
        return Trade(**row)

    async def get_active_positions(self, profile_id: int) -> List[Trade]:
        rows = await self.dm.get_active_positions(profile_id)
        return [self._map_row_to_trade(r) for r in rows]

    async def get_trade_by_order_id(self, order_id: str) -> Optional[Trade]:
        row = await self.dm.get_trade_by_order_id(order_id)
        return self._map_row_to_trade(row) if row else None


    async def get_trade_history(self, profile_id: int, limit: int = 100) -> List[Trade]:
        rows = await self.dm.get_trade_history(profile_id, limit)
        return [self._map_row_to_trade(r) for r in rows]

    async def update_status(self, trade_id: int, status: str, **kwargs) -> None:
        exit_price = kwargs.get('exit_price')
        pnl = kwargs.get('pnl')
        exit_reason = kwargs.get('exit_reason')
        await self.dm.update_position_status(trade_id, status, exit_price, pnl, exit_reason)

    async def get_all_active_trade_profile_ids(self) -> List[int]:
        db = await self.dm.get_db()
        async with db.execute("SELECT DISTINCT profile_id FROM trades WHERE status IN ('ACTIVE', 'OPENED', 'PENDING')") as cursor:
            rows = await cursor.fetchall()
            return [r[0] for r in rows]

    async def get_profile_by_id(self, profile_id: int) -> Optional[Dict[str, Any]]:
        db = await self.dm.get_db()
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None
