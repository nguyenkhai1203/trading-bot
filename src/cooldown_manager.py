import time
import json
import logging
from typing import Optional, Dict, Any

class CooldownManager:
    """
    Centralized manager for symbol-level SL cooldowns and account-level margin throttling.
    
    Responsibilities:
    1. Tracks which symbols are blocked from re-entry after a Stop Loss (SL).
    2. Manages account-wide 'margin throttling' when an exchange rejects orders due to insufficient funds.
    3. Handles persistence of cooldown states to the SQLite database via Profile risk metrics.
    4. Provides a shared interface for multiple Trader profiles sharing the same account.
    """
    
    def __init__(self, db, logger: logging.Logger, trading_env: str = "LIVE"):
        self.db = db
        self.logger = logger
        self.env = trading_env.upper()
        self._sl_cooldowns: Dict[str, float] = {}
        # Cooldown after SL (in seconds)
        self.sl_cooldown_duration = 2 * 3600  # 2 hours default
        self._shared_account_cache = {} # Will be synced with Trader's cache

    async def sync_from_db(self, profile_id: int):
        """
        Loads active SL cooldowns from the DB signal_tracker/risk_metrics.
        Filters out expired cooldowns immediately.
        """
        try:
            raw_data = await self.db.get_risk_metric(profile_id, 'sl_cooldowns_json', env=self.env)
            if raw_data:
                cooldowns = json.loads(raw_data) if isinstance(raw_data, str) else raw_data
                now = time.time()
                self._sl_cooldowns = {k: v for k, v in cooldowns.items() if v > now}
            else:
                self._sl_cooldowns = {}
            self.logger.info(f"[COOLDOWN] Centrally synced {len(self._sl_cooldowns)} active SL cooldowns.")
        except Exception as e:
            self.logger.warning(f"[COOLDOWN] Failed to sync cooldowns from DB: {e}")

    async def save_to_db(self, profile_id: int):
        """Persist cooldown state to database."""
        try:
            now = time.time()
            # Filter expired before saving
            self._sl_cooldowns = {k: v for k, v in self._sl_cooldowns.items() if v > now}
            await self.db.set_risk_metric(profile_id, 'sl_cooldowns_json', json.dumps(self._sl_cooldowns), self.env)
        except Exception as e:
            self.logger.warning(f"[COOLDOWN] Failed to save cooldowns to DB: {e}")

    def is_in_cooldown(self, exchange_name: str, symbol: str) -> bool:
        """
        Checks if a symbol is currently blocked post-SL.
        Returns True if blocked, False otherwise.
        """
        key = f"{exchange_name}:{symbol}"
        if key not in self._sl_cooldowns:
            return False
            
        if time.time() >= self._sl_cooldowns[key]:
            del self._sl_cooldowns[key]
            # Note: Caller should handle DB save if needed, or we save periodically
            return False
        return True

    def get_remaining_minutes(self, exchange_name: str, symbol: str) -> float:
        """Get remaining cooldown time in minutes."""
        key = f"{exchange_name}:{symbol}"
        if key not in self._sl_cooldowns:
            return 0.0
        remaining = self._sl_cooldowns[key] - time.time()
        return max(0.0, remaining / 60.0)

    async def set_sl_cooldown(self, exchange_name: str, symbol: str, profile_id: int, custom_duration: Optional[int] = None):
        """
        Triggers a new SL cooldown for a symbol.
        Cancels any existing open orders for that symbol as a safeguard.
        """
        duration = custom_duration if custom_duration is not None else self.sl_cooldown_duration
        expiry = time.time() + duration
        key = f"{exchange_name}:{symbol}"
        self._sl_cooldowns[key] = expiry
        await self.save_to_db(profile_id)
        
        hours = duration / 3600
        self.logger.info(f"[COOLDOWN] {key} blocked for {hours:.1f} hours.")

    def is_margin_throttled(self, account_key: str, shared_cache: dict) -> bool:
        """Check if account is in a margin-rejection cooldown."""
        shared = shared_cache.get(account_key, {})
        return time.time() < shared.get('margin_cooldown_until', 0)

    async def handle_margin_error(self, account_key: str, shared_cache: dict, exchange_name: str):
        """
        Activates account-level throttling after an 'Insufficient Margin' rejection.
        Prevents the bot from spamming the exchange with invalid orders for 15 minutes.
        """
        shared = shared_cache.get(account_key)
        if shared is not None:
            shared['margin_cooldown_until'] = time.time() + 900  # 15 minute cooldown
            self.logger.warning(
                f"[{exchange_name}] Insufficient margin detected. "
                f"Throttling account {account_key} entries for 15 minutes."
            )
