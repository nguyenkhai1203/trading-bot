import subprocess
import sys
import time

def main():
    print("🚀 Starting bots...")
    # Use sys.executable for better compatibility across OS
    # Pass along any CLI arguments (like --live or --dry-run)
    args = sys.argv[1:]
    trading = subprocess.Popen([sys.executable, 'src/bot.py'] + args)
    time.sleep(10)  # Wait longer for trading bot to initialize
    subprocess.run([sys.executable, 'src/telegram_bot.py'] + args)
    trading.terminate()

if __name__ == '__main__':
    main()