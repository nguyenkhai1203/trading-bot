import subprocess
import sys
import time

def main():
    print("🚀 Starting bots...")
    trading = subprocess.Popen([sys.executable, 'src/bot.py'])
    time.sleep(10)  # Wait longer for trading bot to initialize
    subprocess.run([sys.executable, 'src/telegram_bot.py'])
    trading.terminate()

if __name__ == '__main__':
    main()