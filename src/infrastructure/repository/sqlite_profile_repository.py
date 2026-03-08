from typing import List, Dict, Any, Optional
from src.domain.repository import IProfileRepository
from .database import DataManager

class SQLiteProfileRepository(IProfileRepository):
    def __init__(self, data_manager: DataManager):
        self.dm = data_manager

    async def get_active_profiles(self) -> List[Dict[str, Any]]:
        return await self.dm.get_profiles()

    async def get_profile_by_id(self, profile_id: int) -> Optional[Dict[str, Any]]:
        # DataManager doesn't have get_profile_by_id specifically, common pattern is filter get_profiles
        profiles = await self.dm.get_profiles()
        for p in profiles:
            if p['id'] == profile_id:
                return p
        return None
