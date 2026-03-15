import asyncio
import aiosqlite
import os
from datetime import datetime

async def analyze_trades_v2():
    db_path = "data/trading_live.db"
    if not os.path.exists(db_path):
        print(f"DB not found at {db_path}")
        return

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        # March 1, 2026 00:00:00 UTC
        start_date_ms = int(datetime(2026, 3, 1, 0, 0, 0).timestamp() * 1000)
        
        async with db.execute("SELECT * FROM trades WHERE exit_time >= ? OR (exit_time IS NULL AND status IN ('ACTIVE', 'OPENED', 'PENDING'))", (start_date_ms,)) as cursor:
            rows = await cursor.fetchall()
            
            print(f"Total signals found: {len(rows)}")
            print("-" * 50)
            
            wins = 0
            losses = 0
            skipped = 0
            valid_trades = []
            
            for row in rows:
                t = dict(row)
                status = t.get('status', '').upper()
                exit_reason = (t.get('exit_reason') or '').upper()
                pnl = t.get('pnl') or 0
                
                # Logic from updated telegram_bot.py
                is_technical = exit_reason in ['CANCELLED', 'EVICTED', 'SYNC_ERR', 'SYNC', 'GHOST_SYNC'] or not exit_reason
                if status == 'CANCELLED' or (is_technical and pnl == 0):
                    skipped += 1
                    # print(f"SKIP: {t['symbol']} | Side: {t['side']} | PnL: {pnl} | Reason: {exit_reason}")
                    continue
                
                # If we reach here, it's a valid trade (including REVERSAL with PnL or regular closed trade)
                valid_trades.append(t)
                if pnl > 0:
                    wins += 1
                else:
                    losses += 1
                print(f"VALID: {t['symbol']} | Side: {t['side']} | PnL: {pnl:.4f} | Reason: {exit_reason}")
            
            print("-" * 50)
            print(f"Total Signals: {len(rows)}")
            print(f"Executed (Valid) Trades: {len(valid_trades)}")
            print(f"Wins: {wins}")
            print(f"Losses: {losses}")
            print(f"Skipped: {skipped}")
            if valid_trades:
                print(f"Win Rate: {(wins / len(valid_trades) * 100):.1f}%")

if __name__ == "__main__":
    asyncio.run(analyze_trades_v2())
