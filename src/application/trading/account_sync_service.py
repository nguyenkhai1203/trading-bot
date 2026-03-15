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
                # Prefer profile-specific adapter (by profile_id), fallback to exchange-level adapter
                adapter = self.adapters.get(p.get('id'))
                if not adapter:
                    ex_name = p.get('exchange', '').upper()
                    adapter = self.adapters.get(ex_name)
                if not adapter:
                    self.logger.warning(f"No adapter for profile {p.get('id')} ({p.get('name')}) while sync grouping.")
                    continue
                unique_accounts[key] = {'adapter': adapter, 'profiles': []}
            unique_accounts[key]['profiles'].append(p)

        # 2. Fetch data for each unique account concurrently
        self.logger.info(f"Syncing {len(unique_accounts)} unique accounts from {len(self.profiles)} profiles.")
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
            
            fresh_orders = results[2]
            fresh_order_ids = {str(o.get('id')) for o in fresh_orders if o.get('id')}

            # CRITICAL: Merge optimistic orders that haven't been confirmed by exchange yet.
            # Bybit may take 1-5 seconds to reflect a newly placed order in API.
            # Without this merge, sync_all() would destroy register_pending_order() protection.
            # FIX D2: Compare by order_id (not symbol) to avoid evicting optimistic entries
            # when the account holds other orders on the same coin.
            prev_state = self._state_cache.get(account_key, {})
            surviving_optimistic = [
                o for o in prev_state.get('orders', [])
                if o.get('__optimistic') and str(o.get('id', '')) not in fresh_order_ids
            ]
            
            self._state_cache[account_key] = {
                'balance': results[0],
                'positions': results[1],
                'orders': fresh_orders + surviving_optimistic,
                'timestamp': asyncio.get_event_loop().time()
            }
            self.logger.debug(f"Synced account {account_key} (+{len(surviving_optimistic)} optimistic retained)")
        except Exception as e:
            self.logger.error(f"Failed to sync account {account_key}: {e}")

    def get_account_state(self, profile: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Retrieve the cached state for a specific profile's account."""
        key = self._get_account_key(profile)
        return self._state_cache.get(key)
        
    def get_active_symbols(self) -> List[str]:
        """Collect all symbols currently found in positions or open orders across all accounts."""
        symbols = set()
        for state in self._state_cache.values():
            # Add from positions
            for p in state.get('positions', []):
                sym = p.get('symbol')
                if sym: symbols.add(sym)
            # Add from orders
            for o in state.get('orders', []):
                sym = o.get('symbol')
                if sym: symbols.add(sym)
        return list(symbols)

    def register_pending_order(self, profile: Dict[str, Any], symbol: str, side: str, order_id: str):
        """
        Optimistically injects a newly placed pending order into the local state cache.
        This prevents the guard in execute_trade from being bypassed in the same heartbeat
        cycle where sync_all() hasn't been called yet.
        """
        key = self._get_account_key(profile)
        if key not in self._state_cache:
            self._state_cache[key] = {'balance': {}, 'positions': [], 'orders': [], 'timestamp': 0}
        
        # Inject the order into cache immediately so the next execute_trade guard sees it
        self._state_cache[key]['orders'].append({
            'id': order_id,
            'symbol': symbol,
            'side': side,
            'status': 'OPEN',
            '__optimistic': True  # Mark so we know this was locally added
        })
        self.logger.debug(f"Registered pending order optimistically: {symbol} {side} [{order_id}]")
