import json
import os
import sys
import logging
from datetime import datetime

class RiskManager:
    """
    RiskManager handles circuit breakers, drawdown protection, and position sizing.
    Now integrated with DataManager for persistent SQLite storage.
    """
    def __init__(self, db, profile_id: int, env: str = 'LIVE', exchange_name='BINANCE', 
                 risk_per_trade=0.01, leverage=1, max_drawdown_pct=0.10, daily_loss_limit_pct=0.05):
        self.db = db
        self.profile_id = profile_id
        self.env = env.upper()
        self.exchange_name = exchange_name.upper()
        self.risk_per_trade = risk_per_trade
        self.leverage = leverage
        self.max_drawdown_pct = max_drawdown_pct
        self.daily_loss_limit_pct = daily_loss_limit_pct
        self.logger = logging.getLogger("RiskManager")
        
        self.starting_balance_day = 0
        self.peak_balance = 0
        self._last_reset_date = None  # Track daily reset

    async def sync_from_db(self):
        """Load risk metrics from SQLite database."""
        try:
            self.peak_balance = await self.db.get_risk_metric(self.profile_id, 'peak_balance', self.env) or 0
            self.starting_balance_day = await self.db.get_risk_metric(self.profile_id, 'starting_balance_day', self.env) or 0
            
            reset_date_val = await self.db.get_risk_metric(self.profile_id, 'last_reset_date_val', self.env)
            if reset_date_val:
                # Store as YYYYMMDD float
                year = int(reset_date_val // 10000)
                month = int((reset_date_val % 10000) // 100)
                day = int(reset_date_val % 100)
                self._last_reset_date = datetime(year, month, day).date()
            
            self.logger.info(f"[{self.exchange_name}] Synced risk metrics: Peak={self.peak_balance:.2f}, DayStart={self.starting_balance_day:.2f}")
        except Exception as e:
            self.logger.error(f"Error syncing risk metrics from DB: {e}")

    async def _update_db_metrics(self):
        """Save current metrics to database."""
        try:
            await self.db.set_risk_metric(self.profile_id, 'peak_balance', float(self.peak_balance), self.env)
            await self.db.set_risk_metric(self.profile_id, 'starting_balance_day', float(self.starting_balance_day), self.env)
            
            if self._last_reset_date:
                date_val = self._last_reset_date.year * 10000 + self._last_reset_date.month * 100 + self._last_reset_date.day
                await self.db.set_risk_metric(self.profile_id, 'last_reset_date_val', float(date_val), self.env)
        except Exception as e:
            self.logger.error(f"Error saving risk metrics to DB: {e}")

    async def reset_peak(self, current_balance):
        """Force reset peak to current balance."""
        self.peak_balance = current_balance
        await self._update_db_metrics()
        return f"Peak reset to {current_balance}"

    async def check_circuit_breaker(self, current_balance):
        """
        Returns: (is_stop, reason)
        """
        today = datetime.now().date()
        
        changes = False
        # 1. Daily Tracker Reset
        if self._last_reset_date != today:
            if current_balance > 0:
                self.starting_balance_day = current_balance
                self._last_reset_date = today
                changes = True
                self.logger.info(f"[{self.exchange_name}] Day reset. Starting balance: {self.starting_balance_day:.2f}")
            else:
                self.logger.warning(f"[{self.exchange_name}] Skip day reset: invalid balance {current_balance}")
        
        # 2. Peak Tracker Init
        if self.peak_balance == 0 and current_balance > 0:
            self.peak_balance = current_balance
            changes = True
            
        # 3. Peak Update
        if current_balance > self.peak_balance:
            self.peak_balance = current_balance
            changes = True
            
        if changes:
            await self._update_db_metrics()
            
        # 4. Max Drawdown Protection
        if self.peak_balance > 0:
            drawdown = (self.peak_balance - current_balance) / self.peak_balance
            if drawdown >= self.max_drawdown_pct:
                return True, f"Max Drawdown Hit: {drawdown*100:.2f}% (Peak: {self.peak_balance:.2f}, Curr: {current_balance:.2f})"
            
        # 5. Daily Loss Protection
        if self.starting_balance_day and self.starting_balance_day > 0:
            daily_loss = (self.starting_balance_day - current_balance) / self.starting_balance_day
            if daily_loss >= self.daily_loss_limit_pct:
                return True, f"Daily Loss Limit Hit: {daily_loss*100:.2f}% (Start: {self.starting_balance_day:.2f}, Curr: {current_balance:.2f})"
            
        return False, "OK"

    def calculate_position_size(self, account_balance, entry_price, stop_loss_price, leverage=None, risk_pct=None):
        """Calculates position size with explicit leverage/risk overrides."""
        if entry_price <= 0 or stop_loss_price <= 0:
            return 0
            
        active_risk = risk_pct if risk_pct is not None else self.risk_per_trade
        active_leverage = leverage if leverage is not None else self.leverage
        
        risk_amount = account_balance * active_risk
        price_diff = abs(entry_price - stop_loss_price)
        
        if price_diff == 0:
            return 0

        position_size = risk_amount / price_diff
        position_value = position_size * entry_price
        
        required_margin = position_value / active_leverage
        if required_margin > account_balance:
            max_size = (account_balance * active_leverage) / entry_price
            position_size = min(position_size, max_size)

        return position_size

    def calculate_size_by_cost(self, entry_price, cost_usdt, leverage):
        """Calculates position size given a fixed USDT cost (margin) and leverage."""
        if entry_price <= 0 or cost_usdt <= 0: return 0
        position_value = cost_usdt * leverage
        return position_value / entry_price

    def calculate_sl_tp(self, entry_price, signal_type, atr=None, sl_pct=0.02, tp_pct=0.04):
        """Calculates Stop Loss and Take Profit levels."""
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
