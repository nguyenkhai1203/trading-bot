import asyncio
import logging
import sys
import os

# Add src to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.infrastructure.container import Container
from src.application.trading.trade_orchestrator import TradeOrchestrator
from src import config

async def main():
    # Setup Logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    env_str = 'LIVE' if not config.DRY_RUN else 'TEST'
    print(f"Initializing Clean Architecture Trading Bot [Env: {env_str}]")
    
    # 1. Setup DI Container
    container = await Container.get_instance(env_str)
    
    # 2. Setup Orchestrator
    orchestrator = TradeOrchestrator(container)
    
    # 3. Initialize & Start
    try:
        await orchestrator.initialize()
        await orchestrator.start()
    except KeyboardInterrupt:
        print("\n🛑 Keyboard interrupt received.")
    except Exception as e:
        print(f"💥 Fatal error: {e}")
    finally:
        await orchestrator.stop()

if __name__ == "__main__":
    asyncio.run(main())
