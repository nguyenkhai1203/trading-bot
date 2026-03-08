import asyncio
import logging
from typing import Dict, List, Any, Optional
from src.infrastructure.adapters.base_adapter import BaseAdapter
from src.domain.models import Position, Order

class AccountSyncService:
    """
    Application service to centralize exchange state fetching.
    Groups profiles by physical account to minimize API requests.
    """
    def __init__(self, profiles: List[Dict[str, Any]], adapters: Dict[str, BaseAdapter]):
        self.profiles = profiles
        self.adapters = adapters
        self.logger = logging.getLogger("AccountSyncService")
        # Global cache: {account_key: { 'balance': ..., 'positions': ..., 'orders': ..., 'last_sync': ... }}
        self._state_cache = {}

    def _get_account_key(self, profile: Dict[str, Any]) -> str:
        """Uniquely identify an account by exchange and API key."""
        ex_name = profile.get('exchange', 'UNKNOWN').upper()
        # Use API key if present, otherwise fallback to profile ID for simulation/public
        api_key = profile.get('api_key', f"PROFILE_{profile['id']}")
        return f"{ex_name}_{api_key}"

    async def sync_all(self, force: bool = False):
        """Fetch fresh data for all unique accounts in the system."""
        # 1. Group unique physical accounts
        unique_accounts = {} # {account_key: {'adapter': ..., 'profiles': []}}
        for p in self.profiles:
            key = self._get_account_key(p)
            if key not in unique_accounts:
                # Find matching adapter for this profile's exchange
                ex_name = p.get('exchange', '').upper()
                adapter = self.adapters.get(ex_name)
                if not adapter:
                    continue
                unique_accounts[key] = {'adapter': adapter, 'profiles': []}
            unique_accounts[key]['profiles'].append(p)

        # 2. Fetch data for each unique account concurrently
        tasks = []
        for account_key, data in unique_accounts.items():
            tasks.append(self._sync_account(account_key, data['adapter']))
        
        await asyncio.gather(*tasks)

    async def _sync_account(self, account_key: str, adapter: BaseAdapter):
        """Fetch balance, positions, and orders for a single account."""
        try:
            # concurrently fetch to speed up
            results = await asyncio.gather(
                adapter.fetch_balance(),
                adapter.fetch_positions(),
                adapter.fetch_open_orders()
            )
            
            self._state_cache[account_key] = {
                'balance': results[0],
                'positions': results[1],
                'orders': results[2],
                'timestamp': asyncio.get_event_loop().time()
            }
            self.logger.debug(f"Synced account {account_key}")
        except Exception as e:
            self.logger.error(f"Failed to sync account {account_key}: {e}")

    def get_account_state(self, profile: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Retrieve the cached state for a specific profile's account."""
        key = self._get_account_key(profile)
        return self._state_cache.get(key)
