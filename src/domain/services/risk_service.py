from typing import Dict, Any, List
import logging

class RiskService:
    """
    Domain Service for position sizing, leverage management, and circuit breakers.
    """
    @staticmethod
    def calculate_position_size(balance: float, entry_price: float, stop_loss_price: float, risk_per_trade: float, leverage: float) -> float:
        """
        Calculates position size (quantity) based on balance, risk per trade, and SL distance.
        Clamped by leverage (max notional = balance * leverage).
        """
        if entry_price <= 0 or stop_loss_price <= 0 or entry_price == stop_loss_price:
            return 0.0
            
        risk_amount = balance * risk_per_trade
        price_diff = abs(entry_price - stop_loss_price)
        
        # SL Directional Safety: Avoid tiny diffs or zero division
        if price_diff < (entry_price * 0.0001): 
            return 0.0
            
        # 1. Risk-based quantity
        raw_qty = risk_amount / price_diff
        
        # 2. Leverage-based capping (Max Notional)
        max_notional = balance * leverage
        max_qty = max_notional / entry_price
        
        return min(raw_qty, max_qty)

    @staticmethod
    def calculate_size_by_cost(entry_price: float, cost_usdt: float, leverage: float) -> float:
        """Calculates quantity based on margin cost and leverage."""
        if entry_price <= 0: return 0.0
        return (cost_usdt * leverage) / entry_price

    @staticmethod
    def check_drawdown(current_balance: float, peak_balance: float, max_drawdown_pct: float) -> tuple[bool, str]:
        """Verify if current drawdown exceeds the limit."""
        if peak_balance > 0:
            drawdown = (peak_balance - current_balance) / peak_balance
            if drawdown >= max_drawdown_pct:
                return True, f"Max Drawdown Hit: {drawdown*100:.2f}% (Limit: {max_drawdown_pct*100:.1f}%)"
        return False, "OK"

    @staticmethod
    def calculate_sl_tp(entry_price: float, signal_type: str, atr: float = None, sl_pct: float = 0.02, tp_pct: float = 0.04) -> tuple[float, float]:
        """Calculates SL and TP prices based on percentage or ATR (2.0x for SL, 3.0x for TP)."""
        is_buy = signal_type.upper() in ('BUY', 'LONG')
        
        if atr and atr > 0:
            # ATR-based: SL = 2.0 * ATR, TP = 3.0 * ATR
            if is_buy:
                sl = entry_price - (2.0 * atr)
                tp = entry_price + (3.0 * atr)
            else:
                sl = entry_price + (2.0 * atr)
                tp = entry_price - (3.0 * atr)
        else:
            # Pct-based
            if is_buy:
                sl = entry_price * (1 - sl_pct)
                tp = entry_price * (1 + tp_pct)
            else:
                sl = entry_price * (1 + sl_pct)
                tp = entry_price * (1 - tp_pct)
        return sl, tp
