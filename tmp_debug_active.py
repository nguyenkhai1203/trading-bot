
import asyncio
from src.infrastructure.container import Container

async def main():
    c = Container()
    await c.initialize()
    trades = await c.db_manager.get_active_positions(3)
    print('---START---')
    for t in trades:
        print(f"Trade: {t.get('symbol')} Status: {t.get('status')}")
    print('---END---')
    await c.close()

if __name__ == "__main__":
    asyncio.run(main())
