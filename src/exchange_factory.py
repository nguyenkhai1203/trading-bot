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
    if name == 'BYBIT':
        return BybitAdapter()
    else:
        # Default to Binance
        options = {
            'defaultType': 'future',
            'adjustForTimeDifference': True,
        }
        client = ccxt.binance({
            'apiKey': BINANCE_API_KEY if BINANCE_API_KEY and 'your_' not in BINANCE_API_KEY else None,
            'secret': BINANCE_API_SECRET if BINANCE_API_SECRET and 'your_' not in BINANCE_API_SECRET else None,
            'options': options,
            'enableRateLimit': True,
        })
        return BinanceAdapter(client)

def get_active_exchanges_map():
    """
    Returns a dict {name: adapter} for all exchanges in ACTIVE_EXCHANGES.
    """
    from config import ACTIVE_EXCHANGES
    adapters = {}
    for name in ACTIVE_EXCHANGES:
        name = name.strip()
        if not name: continue
        adapters[name] = get_exchange_adapter(name)
    return adapters
