import ccxt.async_support as ccxt
import aiohttp
from src import config
from src.infrastructure.adapters.binance_adapter import BinanceAdapter
from src.infrastructure.adapters.bybit_adapter import BybitAdapter

def get_exchange_adapter(name=config.ACTIVE_EXCHANGE):
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
        valid_key = config.BINANCE_API_KEY and 'your_' not in config.BINANCE_API_KEY
        valid_secret = config.BINANCE_API_SECRET and 'your_' not in config.BINANCE_API_SECRET
        
        exchange_config = {
            'options': options,
            'enableRateLimit': True,
        }
        
        if valid_key and valid_secret:
            exchange_config['apiKey'] = config.BINANCE_API_KEY
            exchange_config['secret'] = config.BINANCE_API_SECRET
        else:
            print(f"[Factory] Initializing Binance in PUBLIC MODE (No Keys)")
            
        client = ccxt.binance(exchange_config)
        return BinanceAdapter(client)
    else:
        raise ValueError(f"Unknown exchange: {name}")

def get_multi_account_adapters_map(profiles: list):
    """
    Groups profiles by unique physical account and returns a map
    { 'EXCHANGE_APIKEY': adapter }
    """
    adapters = {}
    for p in profiles:
        ex_name = p.get('exchange', 'UNKNOWN').upper()
        api_key = p.get('api_key')
        
        # Unique key for this physical account
        acc_key = f"{ex_name}_{api_key}" if api_key else f"{ex_name}_PUBLIC_{p['id']}"
        
        if acc_key not in adapters:
            try:
                # Use the existing create_adapter_from_profile logic
                # but ensure it returns an adapter with the correct key
                import asyncio
                # Since this is a factory, we might need a sync wrapper if create_adapter_from_profile stays async
                # or just use it directly if we are in an async loop.
                # For safety, we'll implement a sync-friendly creator or keep it async.
                pass
            except Exception as e:
                print(f"[Factory] Failed to create adapter for {acc_key}: {e}")
                
    return adapters

def get_active_exchanges_map():
    """
    LEGACY: Returns a dict {name: adapter} for the FIRST set of keys.
    Mainly used for centralized data fetching (public/main keys).
    """
    adapters = {}
    for name in config.ACTIVE_EXCHANGES:
        name = name.strip().upper()
        if not name: continue
        try:
            adapter = get_exchange_adapter(name)
            
            # Configure Permissions based on PRIMARY keys
            api_key = config.BINANCE_API_KEY if name == 'BINANCE' else config.BYBIT_API_KEY
            has_valid_key = bool(api_key and 'your_' not in api_key)
            adapter.set_permissions(can_trade=has_valid_key, can_view_balance=has_valid_key)
            
            adapters[name] = adapter
            print(f"[Factory] {name}: Initialized as primary adapter.")
        except Exception as e:
            print(f"[Factory] Failed to initialize {name}: {e}")
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
    
    # Create a session with ThreadedResolver to mitigate DNS timeouts
    resolver = aiohttp.ThreadedResolver()
    connector = aiohttp.TCPConnector(resolver=resolver)
    
    if name == 'BYBIT':
        exchange_config = {
            'enableRateLimit': True,
            'connector': connector,
            'options': {
                'defaultType': 'swap',
                'adjustForTimeDifference': True
            }
        }
        if valid_key and valid_secret:
            exchange_config['apiKey'] = api_key
            exchange_config['secret'] = api_secret
        
        client = ccxt.bybit(exchange_config)
        adapter = BybitAdapter(client)
        adapter.set_permissions(can_trade=valid_key, can_view_balance=valid_key)
        # Injection for unique identification (MUST match AccountSyncService._get_account_key)
        adapter.account_key = f"{name}_{api_key}" if api_key else f"{name}_PUBLIC_{profile_dict.get('id', 'global')}"
        return adapter
        
    elif name == 'BINANCE':
        options = {
            'defaultType': 'future',
            'adjustForTimeDifference': True,
            'warnOnFetchOpenOrdersWithoutSymbol': False,
        }
        exchange_config = {
            'connector': connector,
            'options': options,
            'enableRateLimit': True,
        }
        if valid_key and valid_secret:
            exchange_config['apiKey'] = api_key
            exchange_config['secret'] = api_secret
            
        client = ccxt.binance(exchange_config)
        adapter = BinanceAdapter(client)
        adapter.set_permissions(can_trade=valid_key, can_view_balance=valid_key)
        # Injection for unique identification (MUST match AccountSyncService._get_account_key)
        adapter.account_key = f"{name}_{api_key}" if api_key else f"{name}_PUBLIC_{profile_dict.get('id', 'global')}"
        return adapter
    
    return None
