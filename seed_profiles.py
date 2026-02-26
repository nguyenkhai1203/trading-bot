"""
One-time script to create BINANCE + BYBIT profiles in trading_live.db
Run from project root: python seed_profiles.py
"""
import asyncio
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

if os.name == 'nt':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

async def main():
    from database import DataManager
    from config import BINANCE_API_KEY, BINANCE_API_SECRET, BYBIT_API_KEY, BYBIT_API_SECRET
    db = await DataManager.get_instance('LIVE')

    binance_id = await db.add_profile(
        name="Binance Live",
        env="LIVE",
        exchange="BINANCE",
        label="Main Binance account",
        api_key=BINANCE_API_KEY or "",
        api_secret=BINANCE_API_SECRET or "",
        color="yellow"
    )
    print(f"✅ Binance profile ID: {binance_id} | key={'SET' if BINANCE_API_KEY else 'MISSING'}")

    bybit_id = await db.add_profile(
        name="Bybit Live",
        env="LIVE",
        exchange="BYBIT",
        label="Main Bybit account",
        api_key=BYBIT_API_KEY or "",
        api_secret=BYBIT_API_SECRET or "",
        color="cyan"
    )
    print(f"✅ Bybit profile ID: {bybit_id} | key={'SET' if BYBIT_API_KEY else 'MISSING'}")

    profiles = await db.get_profiles()
    print(f"\n📋 All active profiles ({len(profiles)}):")
    for p in profiles:
        print(f"  [{p['id']}] {p['name']} | {p['exchange']} | {p['environment']} | key={'SET' if p.get('api_key') else 'EMPTY'}")

asyncio.run(main())
