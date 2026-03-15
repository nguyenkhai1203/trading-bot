import asyncio
import aiosqlite
import os

async def check_profile_0():
    db_path = 'data/trading_live.db'
    if not os.path.exists(db_path):
        print("DB not found")
        return
    db = await aiosqlite.connect(db_path)
    async with db.execute('SELECT COUNT(*) FROM trades WHERE profile_id = 0') as c:
        row = await c.fetchone()
        print(f"Trades for profile 0: {row[0]}")
    await db.close()

if __name__ == "__main__":
    asyncio.run(check_profile_0())
