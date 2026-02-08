import requests
import time
import os
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv('TELEGRAM_TOKEN')

def get_chat_id():
    if not TOKEN:
        print("Error: TELEGRAM_TOKEN not found in .env")
        return

    print(f"Polling for updates using token: {TOKEN[:5]}...")
    print("Please send a message (e.g., 'Hello') to your bot on Telegram NOW.")
    
    
    url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
    
    for i in range(60): # Try for 60 seconds
        try:
            response = requests.get(url, timeout=5)
            data = response.json()
            
            if data['ok'] and data['result']:
                # Get the last message
                last_msg = data['result'][-1]
                chat_id = last_msg['message']['chat']['id']
                user = last_msg['message']['from']['first_name']
                text = last_msg['message'].get('text', '')
                
                print("\nSUCCESS! Found a message:")
                print(f"From: {user}")
                print(f"Text: {text}")
                print(f"YOUR CHAT ID is: {chat_id}")
                print("--------------------------------------------------")
                print(f"Please update your .env file with: TELEGRAM_CHAT_ID={chat_id}")
                return
            
        except Exception as e:
            print(f"Error: {e}")
            
        time.sleep(1)
        print(".", end="", flush=True)

    print("\nNo messages found. Did you send a message to the bot?")

if __name__ == "__main__":
    get_chat_id()
