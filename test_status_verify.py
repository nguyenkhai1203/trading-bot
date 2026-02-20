import asyncio
import sys
import os

# Add src to path
sys.path.append(os.path.join(os.path.dirname(__file__), 'src'))

from telegram_bot import get_status_message, close

async def test_status():
    print("Generating status message (v2)...")
    try:
        # Force live to avoid cache and see actual merging
        msg = await get_status_message(force_live=True)
        print("\n--- STATUS MESSAGE START ---")
        print(msg)
        print("--- STATUS MESSAGE END ---\n")
    except Exception as e:
        print(f"Error: {e}")
    finally:
        await close()

if __name__ == "__main__":
    asyncio.run(test_status())
