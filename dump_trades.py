import asyncio
import os
import sys
import json
import sqlite3

db_path = "d:/code/tradingBot/data/trading_live.db"
if not os.path.exists(db_path):
    print("DB not found:", db_path)
    sys.exit(1)

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

cursor.execute("SELECT id, profile_id, exchange, symbol, side, status, entry_time, exit_reason FROM trades ORDER BY id DESC LIMIT 50")
rows = cursor.fetchall()
print("Top 50 Recent Trades:")
for r in rows:
    print(r)

conn.close()
