import sqlite3
import os

db_path = "data/trading_live.db"
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

print("--- PROFILES LIST ---")
profiles = conn.execute("SELECT id, name, exchange, environment, is_active FROM profiles").fetchall()
for p in profiles:
    print(dict(p))

print("\n--- RECENT BINANCE TRADES ---")
trades = conn.execute("""
    SELECT symbol, side, status, profile_id, entry_time, pnl, meta_json 
    FROM trades 
    WHERE exchange = 'BINANCE' 
    ORDER BY entry_time DESC 
    LIMIT 20
""").fetchall()

for t in trades:
    print(dict(t))

conn.close()
