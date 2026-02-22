import pytest
import pandas as pd
from backtester import Backtester

class TestBacktester:
    def test_backtester_metrics_calculation(self):
        """Test that the backtester correctly calculates statistical metrics from simulated trades."""
        bt = Backtester(symbol="BTC/USDT", timeframe="1h", exchange="BINANCE", initial_balance=10000)
        
        # Create fake trades that yield predictable metrics
        trades = [
            {'pnl': 100.0},
            {'pnl': 200.0},
            {'pnl': -50.0},
            {'pnl': 150.0},
            {'pnl': -100.0}
        ]
        df_trades = pd.DataFrame(trades)
        
        # Simulate equity curve for plotting drawdown
        bt.equity_curve = [
            {'equity': 10000},
            {'equity': 10100},
            {'equity': 10300},
            {'equity': 10250},
            {'equity': 10400},
            {'equity': 10300}
        ]
        bt.balance = 10300
        
        metrics = bt._calculate_metrics(df_trades)
        
        assert metrics['trades'] == 5
        assert metrics['win_rate'] == 60.0 # 3 wins out of 5 trades
        assert metrics['total_pnl'] == 300.0 # (100+200+150) - (50+100)
        assert metrics['largest_win'] == 200.0
        assert metrics['largest_loss'] == -100.0
        assert metrics['max_consecutive_wins'] == 2 # 100, then 200
        assert metrics['max_consecutive_losses'] == 1 # Losses are interrupted by wins
        
        # Profit factor: gross_profit / ABS(gross_loss) = 450 / 150 = 3.0
        assert metrics['profit_factor'] == 3.0
        
        # ROI: (10300 - 10000) / 10000 * 100 = 3.0%
        assert float(metrics['roi']) == 3.0
        
        # Drawdown logic: max_drawdown is the deepest dip percentage from the highwater mark.
        # Highwater Marks (Max): 10000, 10100, 10300, 10300, 10400, 10400
        # Dipping to 10300 from 10400 is (10300-10400)/10400 = -0.009615 (-0.9615%)
        # Dipping to 10250 from 10300 is (10250-10300)/10300 = -0.004854 (-0.4854%)
        # Max DB should be ~ -0.962%
        assert round(metrics['max_drawdown'], 3) == -0.962

    def test_backtester_close_position_friction(self):
        """Test that commission and slippage correctly eat into gross P&L."""
        bt = Backtester(symbol="BTC/USDT", timeframe="1h", initial_balance=10000, commission=0.001, slippage=0.001)
        
        # Basic position: LONG 10 BTC at $100
        bt.position = {
            'type': 'long',
            'entry_price': 100.0,
            'qty': 10.0, # Total entry notional $1,000
            'entry_time': 0,
            'sl': 90,
            'tp': 110
        }
        
        # Simulation: Closing at $110 (Winning Trade)
        
        # Slippage penalty: 100 * 0.001 = 0.1 -> Eff_Entry = 100.1
        # Slippage penalty: 110 * 0.001 = 0.11 -> Eff_Exit = 109.89
        # Raw PnL = (109.89 - 100.1) * 10 = 9.79 * 10 = 97.9
        
        # Commission penalty (calculated on EFF numbers):
        # Entry Comm: 10 * 100.1 * 0.001 = 1.001
        # Exit Comm: 10 * 109.89 * 0.001 = 1.0989
        # Total Comm = 2.0999
        
        # Net PnL = 97.9 - 2.0999 = 95.8001
        
        bt._close_position(time=1, price=110.0, reason='TP')
        
        assert len(bt.trades) == 1
        trade = bt.trades[0]
        
        assert round(trade['pnl'], 4) == 95.8001
        assert round(bt.balance, 4) == 10095.8001
