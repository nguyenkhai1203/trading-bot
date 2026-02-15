import os
from dotenv import load_dotenv

# Try loading from .env
load_dotenv()

bybit_key = os.getenv('BYBIT_API_KEY')
bybit_secret = os.getenv('BYBIT_API_SECRET')
active_exchange = os.getenv('ACTIVE_EXCHANGE')

print(f"Current Working Directory: {os.getcwd()}")
print(f".env exists: {os.path.exists('.env')}")
print(f"BYBIT_API_KEY present: {bool(bybit_key)}")
if bybit_key:
    print(f"BYBIT_API_KEY length: {len(bybit_key)}")
    print(f"BYBIT_API_KEY preview: {bybit_key[:4]}...{bybit_key[-4:]}")

print(f"BYBIT_API_SECRET present: {bool(bybit_secret)}")
print(f"ACTIVE_EXCHANGE: {active_exchange}")
