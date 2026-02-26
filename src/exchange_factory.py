import ccxt.async_support as ccxt
from config import (
    ACTIVE_EXCHANGE,
    BINANCE_API_KEY, BINANCE_API_SECRET,
    BYBIT_API_KEY, BYBIT_API_SECRET
)
from adapters.binance_adapter import BinanceAdapter
from adapters.bybit_adapter import BybitAdapter

def get_exchange_adapter(name=ACTIVE_EXCHANGE):
    """
    Returns the appropriate Exchange Adapter based on name.
    """
    norm_name = name.upper()
    if norm_name == 'BYBIT':
        return BybitAdapter()
    elif norm_name == 'BINANCE':
        # Default to Binance
        options = {
            'defaultType': 'future',
            'adjustForTimeDifference': True,
            'warnOnFetchOpenOrdersWithoutSymbol': False,
        }
        
        # Determine valid credentials
        valid_key = BINANCE_API_KEY and 'your_' not in BINANCE_API_KEY
        valid_secret = BINANCE_API_SECRET and 'your_' not in BINANCE_API_SECRET
        
        exchange_config = {
            'options': options,
            'enableRateLimit': True,
        }
        
        if valid_key and valid_secret:
            exchange_config['apiKey'] = BINANCE_API_KEY
            exchange_config['secret'] = BINANCE_API_SECRET
        else:
            print(f"⚠️ [Factory] Initializing Binance in PUBLIC MODE (No Keys)")
            
        client = ccxt.binance(exchange_config)
        return BinanceAdapter(client)
    else:
        raise ValueError(f"Unknown exchange: {name}")

def get_active_exchanges_map():
    """
    Returns a dict {name: adapter} for all exchanges in ACTIVE_EXCHANGES.
    """
    from config import ACTIVE_EXCHANGES
    adapters = {}
    for name in ACTIVE_EXCHANGES:
        name = name.strip().upper()
        if not name: continue
        
        # Check for credentials before initializing
        if name == 'BINANCE':
            if not BINANCE_API_KEY or 'your_' in BINANCE_API_KEY:
                print(f"📡 [Factory] BINANCE: Active (Public Mode - Trading Disabled)")
        elif name == 'BYBIT':
            if not BYBIT_API_KEY or 'your_' in BYBIT_API_KEY:
                print(f"📡 [Factory] BYBIT: Active (Public Mode - Trading Disabled)")

                
        try:
            adapter = get_exchange_adapter(name)
            
            # Configure Permissions based on keys
            api_key = BINANCE_API_KEY if name == 'BINANCE' else BYBIT_API_KEY
            
            # Check if key is valid (simple check: present and not default placeholder)
            has_valid_key = bool(api_key and 'your_' not in api_key)
            
            adapter.set_permissions(can_trade=has_valid_key, can_view_balance=has_valid_key)
            
            if adapter.is_public_only:
                print(f"📡 [Factory] {name}: Active (Public Mode - Trading Disabled)")
            else:
                print(f"🔐 [Factory] {name}: Active (Trading Enabled)")
                
            adapters[name] = adapter
            
        except Exception as e:
            print(f"❌ [Factory] Failed to initialize {name}: {e}")
            
            
    return adapters

async def create_adapter_from_profile(profile_dict):
    """
    Creates and initializes an exchange adapter from a profile dictionary.
    """
    name = profile_dict.get('exchange', '').upper()
    api_key = profile_dict.get('api_key')
    api_secret = profile_dict.get('api_secret')
    
    # Validation logic same as get_exchange_adapter but using profile data
    valid_key = api_key and 'your_' not in api_key
    valid_secret = api_secret and 'your_' not in api_secret
    
    if name == 'BYBIT':
        exchange_config = {
            'enableRateLimit': True,
        }
        if valid_key and valid_secret:
            exchange_config['apiKey'] = api_key
            exchange_config['secret'] = api_secret
        
        client = ccxt.bybit(exchange_config)
        adapter = BybitAdapter(client)
        adapter.set_permissions(can_trade=valid_key, can_view_balance=valid_key)
        return adapter
        
    elif name == 'BINANCE':
        options = {
            'defaultType': 'future',
            'adjustForTimeDifference': True,
            'warnOnFetchOpenOrdersWithoutSymbol': False,
        }
        exchange_config = {
            'options': options,
            'enableRateLimit': True,
        }
        if valid_key and valid_secret:
            exchange_config['apiKey'] = api_key
            exchange_config['secret'] = api_secret
            
        client = ccxt.binance(exchange_config)
        adapter = BinanceAdapter(client)
        adapter.set_permissions(can_trade=valid_key, can_view_balance=valid_key)
        return adapter
    
    return None
