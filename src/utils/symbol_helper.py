# -*- coding: utf-8 -*-
"""
Symbol Helper Utility
Centralizes symbol normalization and formatting logic for all exchanges.
"""

def to_api_format(symbol: str) -> str:
    """
    Standardize CCXT symbol for exchange API calls and file paths.
    Eliminates separators and splits suffixes.
    Example: BTC/USDT:USDT -> BTCUSDT
    
    Args:
        symbol (str): Raw symbol string.
        
    Returns:
        str: Cleaned alphanumeric symbol (e.g., 'BTCUSDT').
    """
    if not symbol: return ""
    base = symbol.split(':')[0]
    return base.replace('/', '').upper()

def to_raw_format(symbol: str) -> str:
    """
    Standardize symbol to raw format (no separators, all upper).
    Useful for cross-exchange matching and internal comparisons.
    Example: BTC/USDT:USDT -> BTCUSDT
    
    Args:
        symbol (str): Symbol string.
        
    Returns:
        str: Raw alphanumeric string in uppercase.
    """
    if not symbol: return ""
    return symbol.replace('/', '').replace(':', '').replace('-', '').replace('_', '').upper()

def to_display_format(symbol: str) -> str:
    """
    Standardize symbol for display/notifications.
    Ensures consistent 'BTC/USDT' style.
    Example: BTCUSDT -> BTC/USDT
    
    Args:
        symbol (str): Symbol string.
        
    Returns:
        str: Formatted symbol (e.g., 'BTC/USDT').
    """
    if not symbol: return ""
    # Strip CCXT specific suffixes first
    clean = symbol.split(':')[0].upper()
    if '/' not in clean:
        # Heuristic: Find split point if missing (assume USDT for now)
        if clean.endswith('USDT'):
            return f"{clean[:-4]}/USDT"
    return clean

def get_base_currency(symbol: str) -> str:
    """
    Extract base currency from symbol.
    Example: BTC/USDT -> BTC
    """
    if not symbol: return ""
    return symbol.split('/')[0].upper()

def get_quote_currency(symbol: str) -> str:
    """
    Extract quote currency from symbol.
    Example: BTC/USDT -> USDT
    """
    if not symbol: return ""
    # Handle both BTC/USDT and BTC/USDT:USDT
    base_part = symbol.split(':')[0]
    if '/' in base_part:
        return base_part.split('/')[1].upper()
    return "USDT" # Fallback
