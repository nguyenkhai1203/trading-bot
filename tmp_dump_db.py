
import asyncio
from src.infrastructure.container import Container

async def main():
    c = Container()
    await c.initialize()
    db = await c.db_manager.get_db()
    
    print("---ALL TRADES IN DB---")
    async with db.execute("SELECT id, profile_id, symbol, status, entry_price, sl_price, tp_price FROM trades ORDER BY id DESC LIMIT 50") as cursor:
        rows = await cursor.fetchall()
        for r in rows:
            print(f"ID: {r[0]}, Profile: {r[1]}, Symbol: {r[2]}, Status: {r[3]}, EP: {r[4]}, SL: {r[5]}, TP: {r[6]}")
            
    print("---END---")
    await c.close()

if __name__ == "__main__":
    asyncio.run(main())
