import sqlite3
import os

def check_and_clear():
    db_path = 'data/trading_live.db'
    if not os.path.exists(db_path):
        print("DB not found")
        return

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # What tables exist?
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = cursor.fetchall()
    print("Tables:", tables)
    
    # Clear trades related tables
    for table_tuple in tables:
        table_name = table_tuple[0]
        if table_name in ['trades', 'closed_trades', 'position_history', 'trade_history', 'active_trades']:
            cursor.execute(f"DELETE FROM {table_name}")
            print(f"Cleared {table_name}")
            
    conn.commit()
    conn.close()

if __name__ == '__main__':
    check_and_clear()
