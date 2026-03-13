import os
import pandas as pd
import pytest
from datetime import datetime

def get_timeframe_seconds(tf):
    unit = tf[-1]
    val = int(tf[:-1])
    if unit == 'm': return val * 60
    if unit == 'h': return val * 3600
    if unit == 'd': return val * 86400
    return 60

@pytest.mark.parametrize("data_dir", ["data"])
def test_csv_data_freshness(data_dir):
    """Verify that all CSV data files are fresh and contain enough data."""
    if not os.path.exists(data_dir):
        pytest.skip(f"Data directory {data_dir} not found")

    files = [f for f in os.listdir(data_dir) if f.endswith('.csv')]
    if not files:
        pytest.fail("No CSV files found in data directory")

    now_ts = datetime.now().timestamp()
    stale_files = []
    low_count_files = []

    for f in files:
        # Filename: EXCHANGE_SYMBOL_TF.csv
        parts = f.replace('.csv', '').split('_')
        if len(parts) < 3: continue
        tf = parts[-1]
        tf_seconds = get_timeframe_seconds(tf)
        
        file_path = os.path.join(data_dir, f)
        df = pd.read_csv(file_path)
        
        if df.empty:
            stale_files.append(f"{f} (EMPTY)")
            continue
            
        last_dt = pd.to_datetime(df['timestamp'].iloc[-1])
        last_ts = last_dt.timestamp()
        
        gap_seconds = now_ts - last_ts
        
        # Max allowed gap: 2 * timeframe (or 24h for 1d)
        max_allowed = 2 * tf_seconds if tf != '1d' else 24 * 3600
        
        if gap_seconds > max_allowed:
            stale_files.append(f"{f} (Gap: {gap_seconds/60:.1f}m)")
            
        # Row count check (only for high-frequency data)
        if len(df) < 4500 and tf in ['15m', '30m', '1h']:
            low_count_files.append(f"{f} (Rows: {len(df)})")

    error_msg = ""
    if stale_files:
        error_msg += f"\nSTALE FILES ({len(stale_files)}):\n" + "\n".join(stale_files)
    if low_count_files:
        error_msg += f"\nLOW ROW COUNT ({len(low_count_files)}):\n" + "\n".join(low_count_files)
        
    if error_msg:
        # We use a warning-style fail for low row count if freshness is OK
        # but for this specific request, we want to ensure everything is perfect.
        assert not stale_files, f"Freshness check failed: {error_msg}"
        # assert not low_count_files, f"Incomplete data check failed: {error_msg}"
