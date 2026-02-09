import json
import os

def analyze_history():
    history_path = 'src/trade_history.json'
    if not os.path.exists(history_path):
        print("Trade history file not found.")
        return

    with open(history_path, 'r') as f:
        history = json.load(f)

    if not history:
        print("No trade history available.")
        return

    total_trades = len(history)
    total_pnl = sum(t.get('pnl_usdt', 0) for t in history)
    wins = [t for t in history if t.get('pnl_usdt', 0) > 0]
    losses = [t for t in history if t.get('pnl_usdt', 0) <= 0]
    
    num_wins = len(wins)
    num_losses = len(losses)
    win_rate = (num_wins / total_trades * 100) if total_trades > 0 else 0
    
    total_profit = sum(t.get('pnl_usdt', 0) for t in wins)
    total_loss = sum(t.get('pnl_usdt', 0) for t in losses)
    
    avg_pnl = total_pnl / total_trades if total_trades > 0 else 0
    
    best_trade = max(history, key=lambda x: x.get('pnl_usdt', 0))
    worst_trade = min(history, key=lambda x: x.get('pnl_usdt', 0))

    print("📊 --- TRADE HISTORY ANALYSIS --- 📊")
    print(f"Total Trades: {total_trades}")
    print(f"Win Rate:     {win_rate:.2f}% ({num_wins} Wins / {num_losses} Losses)")
    print(f"Total PnL:    ${total_pnl:.2f}")
    print(f"Total Profit: ${total_profit:.2f}")
    print(f"Total Loss:   ${total_loss:.2f}")
    print(f"Average PnL:  ${avg_pnl:.2f}")
    print(f"Best Trade:   ${best_trade.get('pnl_usdt'):.2f} ({best_trade.get('symbol')})")
    print(f"Worst Trade:  ${worst_trade.get('pnl_usdt'):.2f} ({worst_trade.get('symbol')})")
    print("-----------------------------------")

if __name__ == "__main__":
    analyze_history()
