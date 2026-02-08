class RiskManager:
    def __init__(self, risk_per_trade=0.01, leverage=1, max_drawdown_pct=0.10, daily_loss_limit_pct=0.03):
        self.risk_per_trade = risk_per_trade
        self.leverage = leverage
        self.max_drawdown_pct = max_drawdown_pct
        self.daily_loss_limit_pct = daily_loss_limit_pct
        
        self.starting_balance_day = None
        self.peak_balance = 0

    def check_circuit_breaker(self, current_balance):
        """
        Returns: True if trading should STOP, False otherwise.
        """
        # Init trackers if needed
        if self.starting_balance_day is None:
            self.starting_balance_day = current_balance
            self.peak_balance = current_balance
            
        # Update Peak
        if current_balance > self.peak_balance:
            self.peak_balance = current_balance
            
        # 1. Max Drawdown Check (from Peak)
        drawdown = (self.peak_balance - current_balance) / self.peak_balance
        if drawdown >= self.max_drawdown_pct:
            return True, f"Max Drawdown Hit: {drawdown*100:.2f}%"
            
        # 2. Daily Loss Limit (from Day Start)
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
        if entry_price <= 0: return 0
        
        position_value = cost_usdt * leverage
        qty = position_value / entry_price
        return qty

    def calculate_sl_tp(self, entry_price, signal_type, atr=None, sl_dist_pct=0.02, tp_dist_pct=0.04):
        """
        Calculates Stop Loss and Take Profit levels.
        """
        if signal_type == 'BUY':
            if atr:
                 sl = entry_price - (atr * 2)
                 tp = entry_price + (atr * 3)
            else:
                sl = entry_price * (1 - sl_dist_pct)
                tp = entry_price * (1 + tp_dist_pct)
        elif signal_type == 'SELL':
            if atr:
                sl = entry_price + (atr * 2)
                tp = entry_price - (atr * 3)
            else:
                sl = entry_price * (1 + sl_dist_pct)
                tp = entry_price * (1 - tp_dist_pct)
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
