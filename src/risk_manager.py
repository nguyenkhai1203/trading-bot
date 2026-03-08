import json
import os
import sys
import logging
from datetime import datetime

from src import config
from src.domain.services.risk_service import RiskService

class RiskManager:
    """
    RiskManager handles circuit breakers, drawdown protection, and position sizing.
    Now delegates core logic to RiskService.
    """
    def __init__(self, db, profile_id: int, env: str = 'LIVE', exchange_name='BINANCE', 
                 risk_per_trade=None, leverage=None, max_drawdown_pct=0.10, daily_loss_limit_pct=None):
        self.db = db
        self.profile_id = profile_id
        self.env = env.upper()
        self.exchange_name = exchange_name.upper()
        
        # Use config defaults if not provided
        self.risk_per_trade = risk_per_trade if risk_per_trade is not None else getattr(config, 'RISK_PER_TRADE', 0.01)
        self.leverage = leverage if leverage is not None else getattr(config, 'LEVERAGE', 1)
        self.max_drawdown_pct = max_drawdown_pct
        self.daily_loss_limit_pct = daily_loss_limit_pct if daily_loss_limit_pct is not None else getattr(config, 'DAILY_LOSS_LIMIT_PCT', 0.05)
        self.logger = logging.getLogger("RiskManager")
        
        self.starting_balance_day = 0
        self.peak_balance = 0
        self._last_reset_date = None

    async def sync_from_db(self):
        """Load risk metrics from SQLite database."""
        try:
            self.peak_balance = await self.db.get_risk_metric(self.profile_id, 'peak_balance', self.env) or 0
            self.starting_balance_day = await self.db.get_risk_metric(self.profile_id, 'starting_balance_day', self.env) or 0
            
            reset_date_val = await self.db.get_risk_metric(self.profile_id, 'last_reset_date_val', self.env)
            if reset_date_val:
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

    async def check_circuit_breaker(self, current_balance: float) -> Tuple[bool, str]:
        """State-managed check for circuit breakers."""
        today = datetime.now().date()
        changes = False
        
        if self._last_reset_date != today:
            if current_balance > 0:
                self.starting_balance_day = current_balance
                self._last_reset_date = today
                changes = True
                self.logger.info(f"[{self.exchange_name}] Day reset. Starting balance: {self.starting_balance_day:.2f}")
        
        if self.peak_balance == 0 and current_balance > 0:
            self.peak_balance = current_balance
            changes = True
            
        if current_balance > self.peak_balance:
            self.peak_balance = current_balance
            changes = True
            
        if changes:
            await self._update_db_metrics()
            
        # Call Domain Service for pure logic checks
        is_drawdown, reason = RiskService.check_drawdown(current_balance, self.peak_balance, self.max_drawdown_pct)
        if is_drawdown:
            return True, reason
            
        if self.starting_balance_day > 0:
            daily_loss = (self.starting_balance_day - current_balance) / self.starting_balance_day
            if daily_loss >= self.daily_loss_limit_pct:
                return True, f"Daily Loss Limit Hit: {daily_loss*100:.2f}%"
                
        return False, "OK"

    def calculate_position_size(self, account_balance, entry_price, stop_loss_price, leverage=None, risk_pct=None):
        return RiskService.calculate_position_size(
            account_balance, 
            entry_price, 
            stop_loss_price, 
            risk_pct if risk_pct is not None else self.risk_per_trade,
            leverage if leverage is not None else self.leverage
        )

    def calculate_size_by_cost(self, entry_price, cost_usdt, leverage):
        return RiskService.calculate_size_by_cost(entry_price, cost_usdt, leverage)

    def calculate_sl_tp(self, entry_price, signal_type, atr=None, sl_pct=0.02, tp_pct=0.04):
        return RiskService.calculate_sl_tp(entry_price, signal_type, atr, sl_pct, tp_pct)
