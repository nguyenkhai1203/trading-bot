import subprocess
import sys
import time
import signal
import os

def main():
    print("🚀 Starting Advanced Trading Bot System...")
    
    # 1. Parse Arguments to pass along
    args = sys.argv[1:]
    
    # 2. Launch Trading Engine
    print("   ↳ 📈 Starting Trading Engine (bot.py)...")
    trading_proc = subprocess.Popen([sys.executable, 'src/bot.py'] + args)
    
    # Wait a moment for database initialization
    time.sleep(3)
    
    # 3. Launch Telegram Bot
    print("   ↳ 🤖 Starting Telegram Interface (telegram_bot.py)...")
    telegram_proc = subprocess.Popen([sys.executable, 'src/telegram_bot.py'] + args)
    
    print("\n✅ System fully operational. Press CTRL+C to stop all bots.\n")
    
    # 4. Stay active and monitor
    try:
        while True:
            # Check if processes are still alive
            if trading_proc.poll() is not None:
                print("⚠️ Trading Engine stopped unexpectedly!")
                break
            if telegram_proc.poll() is not None:
                print("⚠️ Telegram Bot stopped unexpectedly!")
                break
            time.sleep(5)
    except KeyboardInterrupt:
        print("\n🛑 Stopping system...")
    finally:
        # 5. Clean termination
        print("   ↳ Terminating processes...")
        telegram_proc.terminate()
        trading_proc.terminate()
        
        # Wait for them to finish
        try:
            telegram_proc.wait(timeout=5)
            trading_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            print("   ↳ (Force killing persistent processes)")
            telegram_proc.kill()
            trading_proc.kill()
            
    print("👋 System shutdown complete.")

if __name__ == '__main__':
    main()