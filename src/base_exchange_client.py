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
        
        # Permissions / Capabilities System
        # Default to restricted until explicitly promoted by Factory
        self.permissions = {
            'can_trade': False,          # Can place/cancel orders
            'can_view_balance': False,   # Can fetch account balance/positions
            'can_use_private': False,    # Can use any private endpoints
            'is_public_only': True       # Is this a read-only observer?
        }

    def set_permissions(self, can_trade: bool, can_view_balance: bool):
        """Configure adapter permissions based on credentials."""
        self.permissions['can_trade'] = can_trade
        self.permissions['can_view_balance'] = can_view_balance
        self.permissions['can_use_private'] = can_trade or can_view_balance
        self.permissions['is_public_only'] = not (can_trade or can_view_balance)
        
    @property
    def can_trade(self): return self.permissions['can_trade']
    
    @property
    def is_public_only(self): return self.permissions['is_public_only']
        
    async def sync_server_time(self) -> bool:
        """Sync local time with exchange server manually to ensure absolute accuracy."""
        try:
            print(f"[TIME SYNC] Fetching raw server time from exchange...")
            server_time = await self.exchange.fetch_time()
            local_time = int(time.time() * 1000)
            
            # Calculate manual offset
            self._server_offset_ms = server_time - local_time
            
            # Also sync CCXT for its internal methods
            await self.exchange.load_time_difference()
            
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

    def get_synced_timestamp(self) -> int:
        """Get current timestamp synchronized with exchange with safety padding."""
        # Use manual offset for absolute control
        local_now = int(time.time() * 1000)
        # We subtract 5000ms safety padding to ensure we are NEVER "ahead" of server
        # (Binance is strict about future timestamps). recvWindow (60s) handles the lag.
        return local_now + self._server_offset_ms - 5000
    
    async def resync_time_if_needed(self, error_msg: str = "") -> bool:
        """Re-sync time if timestamp error detected."""
        if "timestamp" in error_msg.lower() or "time" in error_msg.lower() or "-1021" in error_msg:
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
                    "ahead of the server" in error_msg
                )
                
                if is_timestamp_error and attempt < max_retries - 1:
                    print(f"[TIMESTAMP ERROR] Attempt {attempt + 1}/{max_retries}: {str(e)[:100]}")
                    # Try to resync time
                    await self.resync_time_if_needed(str(e))
                    # Add delay before retry
                    await asyncio.sleep(1)
                    print(f"[RETRY] Retrying API call after time sync...")
                    continue
                else:
                    # Log non-timestamp errors or max retries reached
                    if not is_timestamp_error:
                        # Handle Rate Limit (429) / 418 specifically
                        if "429" in error_msg or "418" in error_msg or "too many requests" in error_msg:
                            wait_s = (attempt + 1) * 5 # Aggressive backoff: 5s, 10s, 15s
                            print(f"⚠️ [RATE LIMIT] backing off for {wait_s}s...")
                            await asyncio.sleep(wait_s)
                            # Retry if we have retries left
                            if attempt < max_retries - 1:
                                continue

                        # Silence known "informational" or handled errors to avoid user confusion
                        silence_errors = [
                            "-4067", "-4046", "-4061", "no change", "side cannot be changed",
                            "last 500 orders", "acknowledged", "already", "not modified"
                        ]
                        if not any(s.lower() in error_msg for s in silence_errors):
                            print(f"[API ERROR] Non-timestamp error, not retrying: {str(e)[:100]}")
                    else:
                        print(f"[TIMESTAMP ERROR] Max retries reached, giving up: {str(e)[:100]}")
                    # Re-raise the error
                    raise e
