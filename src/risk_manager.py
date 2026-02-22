import json
import os
from datetime import datetime

class RiskManager:
    def __init__(self, risk_per_trade=0.01, leverage=1, max_drawdown_pct=0.10, daily_loss_limit_pct=0.03):
        self.risk_per_trade = risk_per_trade
        self.leverage = leverage
        self.max_drawdown_pct = max_drawdown_pct
        self.daily_loss_limit_pct = daily_loss_limit_pct
        
        self.config_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'daily_config.json')
        self.starting_balance_day = None
        self.peak_balance = 0
        self._last_reset_date = None  # Track daily reset
        self._load_daily_config()

    def _load_daily_config(self):
        """Load daily balance and reset date from file."""
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r') as f:
                    data = json.load(f)
                    self.starting_balance_day = data.get('starting_balance_day')
                    self._last_reset_date = data.get('last_reset_date')
                    if self._last_reset_date:
                        self._last_reset_date = datetime.strptime(self._last_reset_date, '%Y-%m-%d').date()
                    self.peak_balance = data.get('peak_balance', 0)
            except Exception:
                pass

    def _save_daily_config(self):
        """Save daily balance and reset date to file."""
        try:
            data = {
                'starting_balance_day': self.starting_balance_day,
                'last_reset_date': str(self._last_reset_date) if self._last_reset_date else None,
                'peak_balance': self.peak_balance
            }
            with open(self.config_file, 'w') as f:
                json.dump(data, f, indent=4)
        except Exception:
            pass

    def reset_peak(self, current_balance):
        """Force reset peak to current balance (use after clearing data)."""
        self.peak_balance = current_balance
        self._save_daily_config()
        return f"Peak reset to {current_balance}"

    def check_circuit_breaker(self, current_balance):
        """
        Returns: True if trading should STOP, False otherwise.
        """
        today = datetime.now().date()
        
        # Reset daily tracker at midnight
        if self._last_reset_date != today:
            self.starting_balance_day = current_balance
            self._last_reset_date = today
            self._save_daily_config()
        
        # Init peak tracker if needed
        if self.peak_balance == 0:
            self.peak_balance = current_balance
            self._save_daily_config()
            
        # Update Peak
        if current_balance > self.peak_balance:
            self.peak_balance = current_balance
            self._save_daily_config()
            
        # 1. Max Drawdown Check (from Peak)
        if self.peak_balance > 0:
            drawdown = (self.peak_balance - current_balance) / self.peak_balance
            if drawdown >= self.max_drawdown_pct:
                return True, f"Max Drawdown Hit: {drawdown*100:.2f}%"
            
        # 2. Daily Loss Limit (from Day Start)
        if self.starting_balance_day and self.starting_balance_day > 0:
            daily_loss = (self.starting_balance_day - current_balance) / self.starting_balance_day
            if daily_loss >= self.daily_loss_limit_pct:
                return True, f"Daily Loss Limit Hit: {daily_loss*100:.2f}%"
            
        return False, "OK"

    
    def calculate_position_size(self, account_balance, entry_price, stop_loss_price, leverage=None, risk_pct=None):
        """
        Calculates position size with explicit leverage/risk overrides.
        """
        if entry_price <= 0 or stop_loss_price <= 0:
            return 0
            
        # Use provided values or class defaults
        active_risk = risk_pct if risk_pct is not None else self.risk_per_trade
        active_leverage = leverage if leverage is not None else self.leverage
        
        risk_amount = account_balance * active_risk
        price_diff = abs(entry_price - stop_loss_price)
        
        if price_diff == 0:
            return 0

        # Position Size = Risk Amount / Price Difference per unit
        position_size = risk_amount / price_diff
        
        # Calculate Value
        position_value = position_size * entry_price
        
        # Margin Check with Active Leverage
        required_margin = position_value / active_leverage
        if required_margin > account_balance:
            # Scale down
            max_size = (account_balance * active_leverage) / entry_price
            position_size = min(position_size, max_size)

        return position_size

    def calculate_size_by_cost(self, entry_price, cost_usdt, leverage):
        """
        Calculates position size given a fixed USDT cost (margin) and leverage.
        Position Value = Cost * Leverage
        Size = Position Value / Price
        """
        if entry_price <= 0 or cost_usdt <= 0: return 0
        
        position_value = cost_usdt * leverage
        qty = position_value / entry_price
        return qty

    def calculate_sl_tp(self, entry_price, signal_type, atr=None, sl_pct=0.02, tp_pct=0.04):
        """
        Calculates Stop Loss and Take Profit levels.
        """
        side = signal_type.upper()
        if side in ['BUY', 'LONG']:
            if atr:
                 sl = entry_price - (atr * 2)
                 tp = entry_price + (atr * 3)
            else:
                sl = entry_price * (1 - sl_pct)
                tp = entry_price * (1 + tp_pct)
        elif side in ['SELL', 'SHORT']:
            if atr:
                sl = entry_price + (atr * 2)
                tp = entry_price - (atr * 3)
            else:
                sl = entry_price * (1 + sl_pct)
                tp = entry_price * (1 - tp_pct)
        else:
            return None, None
            
        return sl, tp

# Test
if __name__ == "__main__":
    rm = RiskManager(risk_per_trade=0.02, leverage=5)
    balance = 1000
    entry = 50000
    sl = 49000
    
    qty = rm.calculate_position_size(balance, entry, sl)
    print(f"Balance: ${balance}, Risk: 2%, Leverage: 5x")
    print(f"Entry: {entry}, SL: {sl} (Diff: {entry-sl})")
    print(f"Calculated Qty: {qty:.4f} BTC")
    print(f"Notional Value: ${qty * entry:.2f}")
    print(f"Required Margin: ${(qty * entry)/5:.2f}")
