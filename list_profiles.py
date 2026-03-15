import asyncio
import aiosqlite
import os

async def list_profiles():
    db_path = 'data/trading_live.db'
    if not os.path.exists(db_path):
        print("DB not found")
        return
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    async with db.execute('SELECT id, name, environment, exchange FROM profiles WHERE is_active=1') as c:
        rows = await c.fetchall()
        for r in rows:
            print(dict(r))
    await db.close()

if __name__ == "__main__":
    asyncio.run(list_profiles())
