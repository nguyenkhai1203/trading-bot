"""
Base Exchange Client - Unified time synchronization and retry logic
This module provides a base class for all components that interact with the exchange.
"""
import asyncio
import time
from typing import Any, Callable


class BaseExchangeClient:
    """
    Base class providing unified exchange interaction patterns.
    Handles time synchronization and timestamp error retries.
    """
    
    def __init__(self, exchange):
        self.exchange = exchange
        self._time_synced = False
        
    async def sync_server_time(self) -> bool:
        """Sync local time with exchange server to fix timestamp offset issues."""
        try:
            # Fetch server time and calculate offset
            server_time_ms = await self.exchange.fetch_time()
            local_time_ms = int(time.time() * 1000)
            offset_ms = server_time_ms - local_time_ms
            
            print(f"[TIME SYNC] Synchronization:")
            print(f"   Server: {server_time_ms}ms, Local: {local_time_ms}ms")
            print(f"   Offset: {offset_ms}ms ({'behind' if offset_ms > 0 else 'ahead'})")
            
            # Set the negative of offset so CCXT adds it to outgoing requests
            time_difference = -offset_ms
            self.exchange.options['timeDifference'] = time_difference
            
            print(f"   Set CCXT timeDifference to: {time_difference}ms")
            print(f"[OK] Time sync complete!")
            self._time_synced = True
            return True
            
        except Exception as e:
            print(f"[WARN] Time sync failed: {str(e)[:100]}")
            print(f"   Using default timeDifference: -2000ms")
            return False
    
    async def resync_time_if_needed(self, error_msg: str = "") -> bool:
        """Re-sync time if timestamp error detected."""
        if "timestamp" in error_msg.lower() or "time" in error_msg.lower():
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
                return await api_call(*args, **kwargs)
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
                        print(f"[API ERROR] Non-timestamp error, not retrying: {str(e)[:100]}")
                    else:
                        print(f"[TIMESTAMP ERROR] Max retries reached, giving up: {str(e)[:100]}")
                    # Re-raise the error
                    raise e
