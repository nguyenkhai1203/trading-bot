import pytest
from unittest.mock import MagicMock, patch
import pandas as pd
from strategy import WeightedScoringStrategy

class TestWeightedScoringStrategy:
    @pytest.fixture
    def strategy(self):
        # Create strategy with default weights and config
        s = WeightedScoringStrategy(symbol="BTC/USDT", timeframe="1h")
        s.weights = {
            'EMA_9_cross_21_up': 4.0,
            'RSI_14_oversold': 3.0,
            'MACD_cross_up': 3.0,
            'EMA_9_cross_21_down': 4.0,
            'RSI_14_overbought': 3.0,
            'MACD_cross_down': 3.0
        }
        s.config_data = {
            'enabled': True,
            'thresholds': {'entry_score': 5.0, 'exit_score': 2.5}
        }
        s.long_signals_cache = {'EMA_9_cross_21_up', 'RSI_14_oversold', 'MACD_cross_up'}
        s.short_signals_cache = {'EMA_9_cross_21_down', 'RSI_14_overbought', 'MACD_cross_down'}
        return s

    def test_get_sizing_tier_default(self, strategy):
        """Verify tier mapping based on score thresholds."""
        # Score < 5 -> Low
        tier_low = strategy.get_sizing_tier(4.0)
        # config.py low tier leverage is 4
        assert tier_low['leverage'] >= 3 
        
        # Score 5-7 -> Medium
        tier_med = strategy.get_sizing_tier(6.0)
        # config.py medium tier leverage is 4
        assert tier_med['leverage'] >= 3
        
        # Score 7+ -> High
        tier_high = strategy.get_sizing_tier(8.0)
        # config.py high tier leverage is 5
        assert tier_high['leverage'] >= 3

    def test_get_signal_basic_scoring(self, strategy):
        """Test basic scoring without brain or adaptive weights."""
        row = {
            'signal_EMA_9_cross_21_up': True,
            'signal_RSI_14_oversold': True,
            'signal_MACD_cross_up': False
        }
        # Expected Score = 4.0 + 3.0 = 7.0
        signal = strategy.get_signal(row, use_adaptive=False, use_brain=False)
        assert signal['side'] == 'BUY'
        assert signal['confidence'] == 7.0

        row_sell = {
            'signal_EMA_9_cross_21_down': True,
            'signal_RSI_14_overbought': True
        }
        # Expected Score = 4.0 + 3.0 = 7.0
        signal_sell = strategy.get_signal(row_sell, use_adaptive=False, use_brain=False)
        assert signal_sell['side'] == 'SELL'
        assert signal_sell['confidence'] == 7.0

    def test_get_signal_adaptive_weights(self, strategy):
        """Verify adaptive weight adjustments via SignalTracker."""
        row = {'signal_EMA_9_cross_21_up': True}
        # Normal weight 4.0
        
        # Mock tracker to boost weight to 6.0
        mock_tracker = MagicMock()
        mock_tracker.adjust_weights.return_value = {'EMA_9_cross_21_up': 6.0}
        
        signal = strategy.get_signal(row, use_adaptive=True, use_brain=False, tracker=mock_tracker)
        assert signal['confidence'] == 6.0
        mock_tracker.adjust_weights.assert_called_once()

    def test_get_signal_brain_veto(self, strategy):
        """Verify Brain VETO blocks a high-confidence signal when neural score is low."""
        strategy.use_brain = True
        strategy.brain = MagicMock()
        strategy.brain.is_trained = True
        strategy.brain.predict.return_value = 0.2 # Below 0.3 threshold
        
        row = {
            'signal_EMA_9_cross_21_up': True,
            'signal_RSI_14_oversold': True
        }
        # Heuristic Score = 7.0 (Buy)
        signal = strategy.get_signal(row, use_adaptive=False, use_brain=True)
        
        assert signal['side'] == 'SKIP'
        assert "Brain VETO" in signal['comment']

    def test_get_signal_brain_boost(self, strategy):
        """Verify Brain BOOST increases confidence when neural score is high."""
        strategy.use_brain = True
        strategy.brain = MagicMock()
        strategy.brain.is_trained = True
        strategy.brain.predict.return_value = 0.9 # Above 0.8 threshold
        
        row = {
            'signal_EMA_9_cross_21_up': True,
            'signal_RSI_14_oversold': True
        }
        # Heuristic Score = 7.0
        signal = strategy.get_signal(row, use_adaptive=False, use_brain=True)
        
        assert signal['side'] == 'BUY'
        # Confidence should be boosted (implementation: min(base_conf * 1.2, 1.0))
        # base_conf = 7.0 / 10.0 = 0.7
        # 0.7 * 1.2 = 0.84
        assert signal['confidence'] == pytest.approx(0.84)

    def test_strategy_disabled(self, strategy):
        """Verify SKIP results when strategy is disabled in config."""
        strategy.config_data['enabled'] = False
        row = {'signal_EMA_9_cross_21_up': True}
        signal = strategy.get_signal(row)
        assert signal['side'] == 'SKIP'
        assert 'Disabled' in signal['comment']
