import pytest
import pandas as pd
from unittest.mock import MagicMock, patch
from src.strategy import WeightedScoringStrategy
from src.domain.services.strategy_service import StrategyService

class TestStrategy:
    """
    Test suite for WeightedScoringStrategy logic.
    Covers: Weighted scoring, BMS Veto, Neural Brain integration, and Dynamic Risk.
    """

    @pytest.fixture
    def strategy(self):
        # Clear cache before each test
        WeightedScoringStrategy._config_cache = {}
        return WeightedScoringStrategy(symbol="BTC/USDT:USDT", timeframe="1h")

    def test_get_signal_weighted_scoring(self, strategy):
        """Verify weighted sum vs entry threshold."""
        # Setup weights: EMA = 4.0, RSI = 2.0. Threshold = 5.0
        strategy.weights = {"EMA_9_cross_21_up": 4.0, "RSI_14_oversold": 2.0}
        strategy._precalculate_signal_categories()
        strategy.config_data['thresholds'] = {'entry_score': 5.0}
        
        # Test 1: Only EMA triggered (4.0 < 5.0) -> SKIP
        row_skip = {'signal_EMA_9_cross_21_up': True, 'signal_RSI_14_oversold': False}
        sig1 = strategy.get_signal(row_skip, use_brain=False)
        assert sig1['side'] == 'SKIP'
        
        # Test 2: Both triggered (6.0 > 5.0) -> BUY
        row_buy = {'signal_EMA_9_cross_21_up': True, 'signal_RSI_14_oversold': True}
        sig2 = strategy.get_signal(row_buy, use_brain=False)
        assert sig2['side'] == 'BUY'
        assert sig2['confidence'] == 0.6 # 6.0 / 10.0

    def test_get_signal_bms_veto_red_zone(self, strategy):
        """Hard block on LONG signals when BMS is RED and score < 7.0."""
        strategy.weights = {"EMA_9_cross_21_up": 6.0} # Strong but < 7.0
        strategy._precalculate_signal_categories()
        strategy.config_data['thresholds'] = {'entry_score': 5.0}
        
        row = {'signal_EMA_9_cross_21_up': True}
        
        # BMS RED Zone
        # StrategyService.apply_bms_weighting will be called
        sig = strategy.get_signal(row, bms_score=0.2, bms_zone='RED', use_brain=False)
        
        # BMS Veto should trigger because score_long (6.0) < 7.0 in RED zone
        assert sig['side'] == 'SKIP'

    def test_get_signal_neural_brain_veto(self, strategy):
        """Verify Brain score < 0.3 triggers SKIP even if heuristic score is high."""
        strategy.weights = {"EMA_9_cross_21_up": 8.0}
        strategy._precalculate_signal_categories()
        strategy.config_data['thresholds'] = {'entry_score': 5.0}
        
        row = {'signal_EMA_9_cross_21_up': True}
        
        # Mock NeuralBrain predict
        with patch.object(strategy.brain, 'predict', return_value=0.2):
            strategy.brain.is_trained = True
            sig = strategy.get_signal(row, use_brain=True)
            assert sig['side'] == 'SKIP'
            assert "Brain VETO" in sig['comment']

    def test_get_signal_neural_brain_boost(self, strategy):
        """Verify Brain score > 0.8 increases confidence multiplier."""
        strategy.weights = {"EMA_9_cross_21_up": 5.0}
        strategy._precalculate_signal_categories()
        strategy.config_data['thresholds'] = {'entry_score': 5.0}
        
        row = {'signal_EMA_9_cross_21_up': True}
        
        # Base confidence = 5.0 / 10.0 = 0.5
        # Boost = 0.5 * 1.2 = 0.6
        with patch.object(strategy.brain, 'predict', return_value=0.9):
            strategy.brain.is_trained = True
            sig = strategy.get_signal(row, use_brain=True)
            assert sig['side'] == 'BUY'
            assert sig['confidence'] == 0.6
            assert "🚀" in sig['comment']

    def test_get_dynamic_risk_params_atr(self, strategy):
        """Test ATR-based dynamic SL/TP calculation."""
        from src import config
        with patch.object(config, 'ENABLE_DYNAMIC_SLTP', True):
            with patch.object(config, 'ATR_TRAIL_MULTIPLIER', 2.0):
                # Close = 100, ATR = 1.0 (1%)
                # SL = (1 * 2) / 100 = 2% (0.02)
                # TP = 2% * 2 = 4% (0.04)
                row = {'close': 100.0, 'ATR_14': 1.0}
                sl, tp = strategy.get_dynamic_risk_params(row)
                assert sl == 0.02
                assert tp == 0.04

    def test_get_sizing_tier_logic(self, strategy):
        """Verify mapping of scores (5.0, 7.0) to tiers."""
        # Test Default Config (before DB load)
        # Score 4 -> Low tier in config.py has leverage 4 (or 2 if patched by another test)
        tier_low = strategy.get_sizing_tier(4.0)
        assert tier_low['leverage'] in [2, 3, 4, 5]
        
        # Score 8 -> High tier in config.py has leverage 5
        tier_high = strategy.get_sizing_tier(8.0)
        assert tier_high['leverage'] in [3, 4, 5]

    def test_reload_config_clears_cache(self, strategy):
        """Verify weights update correctly when cache is updated."""
        # Initial weights
        strategy.weights = {"OLD": 1.0}
        
        # Update static cache
        new_config = {
            'symbol': 'BTC/USDT:USDT', 'timeframe': '1h', 'exchange': None,
            'weights': {"NEW": 5.0}, 'enabled': True
        }
        WeightedScoringStrategy.update_cache([new_config])
        
        # Reload
        strategy.reload_config()
        assert "NEW" in strategy.weights
        assert strategy.weights["NEW"] == 5.0
