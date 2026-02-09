import json
import os
import pandas as pd
from config import TRADING_SYMBOLS, TRADING_TIMEFRAMES

def generate_report():
    config_path = 'src/strategy_config.json'
    positions_path = 'src/positions.json'
    
    if not os.path.exists(config_path):
        print("Strategy config not found.")
        return

    with open(config_path, 'r') as f:
        config = json.load(f)
    
    positions = {}
    if os.path.exists(positions_path):
        with open(positions_path, 'r') as f:
            positions = json.load(f)

    print("\n" + "="*60)
    print("      📊 TRADING BOT STATUS REPORT 📊")
    print("="*60)
    
    active_configs = []
    inactive_configs = []
    
    for key, data in config.items():
        if key == 'default': continue
        
        status = "ON" if data.get('enabled', True) else "OFF"
        weights_count = len([w for w in data.get('weights', {}).values() if w > 0])
        sl = data.get('risk', {}).get('sl_pct', 0.02) * 100
        tp = data.get('risk', {}).get('tp_pct', 0.04) * 100
        score = data.get('thresholds', {}).get('entry_score', 5.0)
        
        perf = data.get('performance', {})
        pnl_sim = perf.get('pnl_sim', 0)
        wr_sim = perf.get('win_rate_sim', 0) * 100
        
        info = {
            'key': key,
            'status': status,
            'weights': weights_count,
            'params': f"Score={score} | SL={sl:.1f}% | TP={tp:.1f}%",
            'backtest': f"+${pnl_sim:>6.2f} | WR: {wr_sim:>5.1f}%"
        }
        
        if status == "ON":
            active_configs.append(info)
        else:
            inactive_configs.append(info)

    print(f"\n✅ ACTIVE SCENARIOS ({len(active_configs)}):")
    print(f"{'Symbol_TF':<20} | {'Weights':<7} | {'Backtest PnL/WR':<18} | {'Parameters'}")
    print("-" * 85)
    for c in active_configs:
        print(f"{c['key']:<20} | {c['weights']:<7} | {c['backtest']:<18} | {c['params']}")

    if inactive_configs:
        print(f"\n❌ DISABLED SCENARIOS ({len(inactive_configs)}):")
        for c in inactive_configs:
            print(f"- {c['key']}")

    print("\n" + "="*60)
    print(f"      💰 OPEN POSITIONS ({len(positions)})")
    print("="*60)
    if not positions:
        print("No active positions.")
    else:
        for key, pos in positions.items():
            print(f"[{key}] {pos['side']} | Qty: {pos['qty']} | Entry: {pos['entry_price']}")

    print("\nUse 'strategy_config.json' to toggle 'enabled': True/False for any scenario.")
    print("="*60 + "\n")

if __name__ == "__main__":
    generate_report()
