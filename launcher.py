import subprocess
import sys
import time

def main():
    print("🚀 Starting bots...")
    # Use 'py' launcher instead of sys.executable for better compatibility
    trading = subprocess.Popen(['py', 'src/bot.py'])
    time.sleep(10)  # Wait longer for trading bot to initialize
    subprocess.run(['py', 'src/telegram_bot.py'])
    trading.terminate()

if __name__ == '__main__':
    main()