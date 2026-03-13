import os
import pandas as pd
from datetime import datetime
import time

def get_timeframe_seconds(tf):
    unit = tf[-1]
    val = int(tf[:-1])
    if unit == 'm': return val * 60
    if unit == 'h': return val * 3600
    if unit == 'd': return val * 86400
    return 60

def verify_data():
    data_dir = 'data'
    if not os.path.exists(data_dir):
        print("[!] Error: 'data' directory not found.")
        return

    files = [f for f in os.listdir(data_dir) if f.endswith('.csv')]
    if not files:
        print("[!] Error: No CSV files found in 'data'.")
        return

    now = datetime.now()
    now_ts = now.timestamp()
    results = []
    
    print(f"[*] Verifying {len(files)} data files (Current Time: {now.strftime('%Y-%m-%d %H:%M:%S')})\n")
    print(f"{'File':<40} | {'Rows':<6} | {'Gap (m)':<8} | {'Status'}")
    print("-" * 80)

    for f in files:
        try:
            # Filename: EXCHANGE_SYMBOL_TF.csv
            parts = f.replace('.csv', '').split('_')
            if len(parts) < 3: continue
            tf = parts[-1]
            tf_seconds = get_timeframe_seconds(tf)
            
            df = pd.read_csv(os.path.join(data_dir, f))
            if df.empty:
                results.append((f, 0, 0, "EMPTY"))
                continue
                
            last_dt = pd.to_datetime(df['timestamp'].iloc[-1])
            last_ts = last_dt.timestamp()
            
            gap_seconds = now_ts - last_ts
            gap_minutes = gap_seconds / 60
            
            # Status: OK if gap < 2 * timeframe
            # We allow a bit more for 1d since Bybit close might be hours ago
            max_allowed = 2 * tf_seconds if tf != '1d' else 24 * 3600
            
            status = "OK" if gap_seconds <= max_allowed else "STALE"
            if len(df) < 4000 and tf not in ['1d', '4h', '8h']: # We expect 5000 for small timeframes
                 status += " (LOW ROW COUNT)"

            print(f"{f:<40} | {len(df):<6} | {gap_minutes:<8.1f} | {status}")
            results.append((f, len(df), gap_minutes, status))
        except Exception as e:
            print(f"{f:<40} | ERROR: {e}")

    stale_count = sum(1 for r in results if "STALE" in r[3])
    low_row_count = sum(1 for r in results if "LOW ROW COUNT" in r[3])
    
    print("\n" + "=" * 80)
    print(f"[*] Verified: {len(results)} files.")
    if stale_count:
        print(f"[!] Warning: {stale_count} files are STALE.")
    if low_row_count:
        print(f"[!] Warning: {low_row_count} files have LOW ROW COUNT.")
    if not stale_count and not low_row_count:
        print("[MATCH] All data is fresh and complete!")
    print("=" * 80)

if __name__ == "__main__":
    verify_data()
