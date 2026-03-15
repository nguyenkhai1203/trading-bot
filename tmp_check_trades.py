
import asyncio
from src.infrastructure.container import Container

async def main():
    c = Container()
    await c.initialize()
    db = await c.db_manager.get_db()
    
    print("---ALL ACTIVE TRADES---")
    async with db.execute("SELECT id, profile_id, symbol, status FROM trades WHERE status IN ('ACTIVE', 'OPENED', 'PENDING')") as cursor:
        rows = await cursor.fetchall()
        for r in rows:
            print(f"ID: {r[0]}, Profile: {r[1]}, Symbol: {r[2]}, Status: {r[3]}")
    
    print("\n---SEARCHING FOR RIVER---")
    async with db.execute("SELECT id, profile_id, symbol, status FROM trades WHERE symbol LIKE '%RIVER%'") as cursor:
        rows = await cursor.fetchall()
        if not rows:
            print("No RIVER trades found in any status.")
        for r in rows:
            print(f"ID: {r[0]}, Profile: {r[1]}, Symbol: {r[2]}, Status: {r[3]}")
            
    print("---END---")
    await c.close()

if __name__ == "__main__":
    asyncio.run(main())
