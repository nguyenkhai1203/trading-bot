import pytest
from datetime import datetime, timedelta
from risk_manager import RiskManager

class TestRiskManager:
    @pytest.fixture
    def rm(self):
        return RiskManager(
            risk_per_trade=0.02, 
            leverage=5, 
            max_drawdown_pct=0.10, 
            daily_loss_limit_pct=0.05
        )

    def test_check_circuit_breaker_daily_loss(self, rm):
        """Verify that daily loss limit stops trading."""
        rm.starting_balance_day = 1000
        rm._last_reset_date = datetime.now().date()
        
        # Loss of 4% (OK)
        stop, msg = rm.check_circuit_breaker(960)
        assert stop is False
        
        # Loss of 6% (Limit hit)
        stop, msg = rm.check_circuit_breaker(940)
        assert stop is True
        assert "Daily Loss Limit" in msg

    def test_check_circuit_breaker_max_drawdown(self, rm):
        """Verify that max drawdown from peak stops trading."""
        rm.peak_balance = 2000
        
        # Balance 1850 (7.5% drawdown, OK)
        stop, msg = rm.check_circuit_breaker(1850)
        assert stop is False
        
        # Balance 1790 (10.5% drawdown, Limit hit)
        stop, msg = rm.check_circuit_breaker(1790)
        assert stop is True
        assert "Max Drawdown" in msg

    def test_daily_reset_logic(self, rm):
        """Verify that daily trackers reset at midnight."""
        yesterday = datetime.now().date() - timedelta(days=1)
        rm._last_reset_date = yesterday
        rm.starting_balance_day = 5000
        
        # Call now - should reset starting_balance_day to current balance
        stop, msg = rm.check_circuit_breaker(1000)
        assert rm.starting_balance_day == 1000
        assert rm._last_reset_date == datetime.now().date()

    def test_calculate_position_size_risk(self, rm):
        """Verify risk-based position sizing."""
        balance = 1000
        entry = 50000
        sl = 49000 # 2% price diff
        
        # Risk Amount = 1000 * 0.02 = 20
        # Qty = 20 / 1000 = 0.02
        qty = rm.calculate_position_size(balance, entry, sl)
        assert qty == pytest.approx(0.02)
        
        # Check leverage clamping
        # Notional = 0.02 * 50000 = 1000. Margin needed at 5x = 200. (Safe)
        
        # Test extreme risk that exceeds account at given leverage
        # Entry 50000, SL 49999. Qty = 20 / 1 = 20. Notional = 1M. Margin needed = 200k.
        # Max Notional at 5x leverage = 1000 * 5 = 5000. Max Size = 5000 / 50000 = 0.1
        qty_clamped = rm.calculate_position_size(balance, entry, 49999)
        assert qty_clamped == pytest.approx(0.1)

    def test_calculate_size_by_cost(self, rm):
        """Verify cost-based sizing."""
        qty = rm.calculate_size_by_cost(entry_price=50000, cost_usdt=10, leverage=5)
        # Position Value = 50. Qty = 50 / 50000 = 0.001
        assert qty == pytest.approx(0.001)

    def test_calculate_sl_tp_pct(self, rm):
        """Verify percentage-based SL/TP."""
        # Long
        sl, tp = rm.calculate_sl_tp(100, 'BUY', sl_pct=0.01, tp_pct=0.02)
        assert sl == 99
        assert tp == 102
        
        # Short
        sl, tp = rm.calculate_sl_tp(100, 'SHORT', sl_pct=0.01, tp_pct=0.02)
        assert sl == 101
        assert tp == 98

    def test_calculate_sl_tp_atr(self, rm):
        """Verify ATR-based SL/TP."""
        # Long: SL = 100 - (2*5) = 90, TP = 100 + (3*5) = 115
        sl, tp = rm.calculate_sl_tp(100, 'LONG', atr=5)
        assert sl == 90
        assert tp == 115
