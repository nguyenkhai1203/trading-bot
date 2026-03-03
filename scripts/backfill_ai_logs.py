import asyncio
import sys
import os
import json
import logging

# Add src to path
sys.path.insert(0, os.path.join(os.getcwd(), 'src'))

from database import DataManager

async def backfill():
    print("🚀 Starting AI training logs backfill...")
    count = 0
    added = 0
    
    try:
        db_manager = await DataManager.get_instance()
        db = await db_manager.get_db()
        
        # Get all trades with metadata
        async with db.execute("SELECT id, meta_json FROM trades WHERE meta_json IS NOT NULL") as cursor:
            rows = await cursor.fetchall()
            
        for row in rows:
            trade_id = row[0]
            try:
                meta = json.loads(row[1])
                snapshot = meta.get('snapshot')
                confidence = meta.get('entry_confidence', 0.5)
                
                if snapshot:
                    count += 1
                    # Check if already exists in ai_training_logs
                    async with db.execute("SELECT 1 FROM ai_training_logs WHERE trade_id = ?", (trade_id,)) as check_cursor:
                        if not await check_cursor.fetchone():
                            await db_manager.log_ai_snapshot(trade_id, json.dumps(snapshot), confidence)
                            added += 1
            except Exception as e:
                print(f"  ⚠️ Error processing trade {trade_id}: {e}")
                
        await db.commit()
        print(f"\n✅ Backfill complete!")
        print(f"📊 Found snapshots: {count}")
        print(f"✨ New logs added: {added}")
        
    except Exception as e:
        print(f"❌ Critical Error: {e}")

if __name__ == "__main__":
    try:
        asyncio.run(backfill())
    finally:
        from database import DataManager
        asyncio.run(DataManager.clear_instances())
