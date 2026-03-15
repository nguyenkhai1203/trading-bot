"""
Base Exchange Client - Unified time synchronization and retry logic
This module provides a base class for all components that interact with the exchange.
"""
import asyncio
import time
from typing import Any, Callable


import logging


class BaseExchangeClient:
    """
    Base class providing unified exchange interaction patterns.
    Handles time synchronization and timestamp error retries.
    """
    
    def __init__(self, exchange):
        self.exchange = exchange
        self.logger = logging.getLogger(self.__class__.__name__)
        self._time_synced = False
        self._server_offset_ms = 0 # Manual offset: serverTime - localTime
        self._sync_lock = asyncio.Lock()
        self._last_sync_time = 0
        
        self.can_trade = False
        self.can_view_balance = False
        self.can_use_private = False

    @property
    def is_public_only(self): 
        return not (self.can_trade or self.can_view_balance)
        
    async def sync_server_time(self) -> bool:
        """Sync local time with exchange server manually to ensure absolute accuracy."""
        if self._sync_lock.locked():
            print(f"[TIME SYNC] Time sync already in progress by another task. Yielding...")
            async with self._sync_lock:
                return True
                
        async with self._sync_lock:
            # Prevent rapid back-to-back synchronization
            if time.time() - self._last_sync_time < 5:
                return True
                
            try:
                print(f"[TIME SYNC] Fetching raw server time from exchange...")
                server_time = await asyncio.wait_for(self.exchange.fetch_time(), timeout=15)
                local_time = int(time.time() * 1000)
                
                # Calculate manual offset
                self._server_offset_ms = server_time - local_time
                
                # Also sync CCXT for its internal methods
                await asyncio.wait_for(self.exchange.load_time_difference(), timeout=15)
                
                # Ensure recvWindow is large (60s is Binance max)
                self.exchange.options['recvWindow'] = 60000 
                
                print(f"[OK] Time Sync Complete!")
                print(f"     Manual Offset: {self._server_offset_ms}ms | CCXT Offset: {self.exchange.options.get('timeDifference', 0)}ms")
                print(f"     Safety Window (recvWindow): 60000ms")
                
                self._time_synced = True
                return True
            except Exception as e:
                print(f"[WARN] Time sync failed: {str(e)[:100]}")
                return False
            finally:
                self._last_sync_time = time.time()

    def get_synced_timestamp(self) -> int:
        """Get current timestamp synchronized with exchange with safety padding."""
        # Use manual offset as primary for absolute control
        local_now = int(time.time() * 1000)
        
        # We subtract 2000ms safety padding (increased from 1000ms) to ensure we are 
        # NOT "ahead" of server even with micro-oscillations.
        # Binance is strict about future timestamps. recvWindow (60s) handles the lag.
        return local_now + self._server_offset_ms - 2000
    
    async def resync_time_if_needed(self, error_msg: str = "") -> bool:
        """Re-sync time if timestamp error detected."""
        if any(x in error_msg.lower() for x in ["timestamp", "-1021", "recvwindow", "10002", "ahead of the server"]):
            print(f"[TIME SYNC] Detected timestamp error, re-syncing...")
            return await self.sync_server_time()
        return False

    async def _execute_with_timestamp_retry(
        self, 
        api_call: Callable, 
        *args, 
        max_retries: int = 3,
        **kwargs
    ) -> Any:
        """
        Execute exchange API call with timestamp error retry.
        
        Args:
            api_call: The async function to call
            *args: Positional arguments for the API call
            max_retries: Maximum number of retry attempts
            **kwargs: Keyword arguments for the API call
            
        Returns:
            Result from the API call
            
        Raises:
            Exception: Re-raises the last exception if all retries fail
        """
        for attempt in range(max_retries):
            try:
                res = await api_call(*args, **kwargs)
                # Double safety: if we somehow got a coroutine back (due to nested calls), await it.
                if asyncio.iscoroutine(res):
                    return await res
                return res
            except Exception as e:
                error_msg = str(e).lower()
                # Check for timestamp-related errors (-1021 is Binance timestamp error code)
                is_timestamp_error = (
                    "timestamp" in error_msg or 
                    "-1021" in error_msg or
                    "10002" in error_msg or
                    "recvwindow" in error_msg or
                    "ahead of the server" in error_msg
                )
                
                if is_timestamp_error and attempt < max_retries - 1:
                    self.logger.warning(f"[TIMESTAMP ERROR] Attempt {attempt + 1}/{max_retries}: {str(e)[:100]}")
                    # Try to resync time
                    await self.resync_time_if_needed(str(e))
                    # Add delay before retry
                    await asyncio.sleep(1)
                    self.logger.info(f"[RETRY] Retrying API call after time sync...")
                    continue
                else:
                    # Log non-timestamp errors or max retries reached
                    if not is_timestamp_error:
                        # Handle Rate Limit (429) / 418 / 403 or Bybit 10006 specifically
                        # EXCLUDE Bybit 10001 (zero position) from backoff as it's a terminal state error
                        if any(x in error_msg for x in ["429", "418", "403", "too many requests", "10006"]) and "10001" not in error_msg:
                            wait_s = (attempt + 1) * 5 # Backoff: 5s, 10s, 15s
                            self.logger.warning(f"[RATE LIMIT/403] backing off for {wait_s}s... Error: {error_msg[:100]}")
                            await asyncio.sleep(wait_s)
                            # Retry if we have retries left
                            if attempt < max_retries - 1:
                                continue

                        # Silence known "informational" or handled errors to avoid user confusion
                        silence_errors = [
                            "side cannot be changed",
                            "last 500 orders", "acknowledged", "already", "not modified",
                            "-2011", "-2013", "order does not exist", "already passed",
                            "fetchpositionmode", "is not supported", "missing some parameters",
                            "reduce-only order qty"
                        ]
                        if not any(s.lower() in error_msg for s in silence_errors):
                            self.logger.error(f"[API ERROR] {type(e).__name__} in {api_call.__name__} for {args}: {str(e)[:250]}")
                    else:
                        self.logger.error(f"[TIMESTAMP ERROR] {api_call.__name__} for {args} Max retries reached: {str(e)[:250]}")
                    # Re-raise the error
                    raise e
