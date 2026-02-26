import pytest
import os
import json
from unittest.mock import MagicMock, patch
from signal_tracker import SignalTracker

class TestSignalTracker:
    @pytest.fixture
    def tracker(self):
        """Create a SignalTracker with a mocked db."""
        mock_db = MagicMock()
        st = SignalTracker(db=mock_db, profile_id=1)
        return st

    @pytest.mark.asyncio
    async def test_record_trade_win_resets_loss_counter(self, tracker):
        """Verify that a winning trade resets the consecutive loss counter."""
        tracker.consecutive_losses = 2
        # record_trade expects result in ('WIN', 'LOSS')
        await tracker.record_trade('BTC/USDT', '1h', 'BUY', ['sig1'], 'WIN', 10.0)
        
        assert tracker.consecutive_losses == 0

    @pytest.mark.asyncio
    async def test_record_trade_loss_increments_counter(self, tracker):
        """Verify that a losing trade increments the consecutive loss counter."""
        tracker.consecutive_losses = 0
        # Use capitalized 'LOSS'
        await tracker.record_trade('BTC/USDT', '1h', 'BUY', ['sig1'], 'LOSS', -10.0)
        
        # Verify it incremented from 0 to 1 (below LOSS_TRIGGER_COUNT=2)
        assert tracker.consecutive_losses == 1

    def test_update_signal_stats(self, tracker):
        """Verify that per-signal performance stats are updated."""
        signals = ['RSI_Oversold', 'EMA_Cross']
        
        # Win
        tracker._update_signal_stats(signals, 'WIN')
        assert tracker.signal_stats['RSI_Oversold']['wins'] == 1
        assert tracker.signal_stats['EMA_Cross']['total'] == 1
        
        # Loss
        tracker._update_signal_stats(signals, 'LOSS')
        assert tracker.signal_stats['RSI_Oversold']['losses'] == 1
        assert tracker.signal_stats['RSI_Oversold']['total'] == 2
        # win_rate is not stored, but we can verify results list
        assert tracker.signal_stats['RSI_Oversold']['recent_results'] == [1, 0]

    def test_get_weight_multiplier(self, tracker):
        """Verify adaptive weight multipliers based on win rate."""
        # Neutral (no trades)
        assert tracker.get_weight_multiplier('NEW_SIGNAL') == 1.0
        
        # Good performance (100% WR over 5 trades)
        # Recent results: 1 = win, 0 = loss
        tracker.signal_stats['WINNER'] = {
            'wins': 5, 'losses': 0, 'total': 5, 
            'recent_results': [1, 1, 1, 1, 1]
        }
        # Multiplier = WEIGHT_BOOST (1.2 from signal_tracker.py)
        assert tracker.get_weight_multiplier('WINNER') == 1.2
        
        # Poor performance (0% WR over 5 trades)
        tracker.signal_stats['LOSER'] = {
            'wins': 0, 'losses': 5, 'total': 5, 
            'recent_results': [0, 0, 0, 0, 0]
        }
        # Multiplier = WEIGHT_PENALTY (0.5 from signal_tracker.py)
        assert tracker.get_weight_multiplier('LOSER') == 0.5

    def test_adjust_weights(self, tracker):
        """Verify that weight dictionary is adjusted correctly."""
        weights = {'SIGNAL_A': 10.0, 'SIGNAL_B': 10.0}
        
        tracker.signal_stats['SIGNAL_A'] = {
            'wins': 10, 'losses': 0, 'total': 10, 
            'recent_results': [1] * 10
        }
        tracker.signal_stats['SIGNAL_B'] = {
            'wins': 0, 'losses': 10, 'total': 10, 
            'recent_results': [0] * 10
        }
        
        adjusted = tracker.adjust_weights(weights)
        
        assert adjusted['SIGNAL_A'] == 12.0 # 10.0 * 1.2
        assert adjusted['SIGNAL_B'] == 5.0  # 10.0 * 0.5

    def test_should_skip_symbol(self, tracker):
        """Verify symbol skipping logic for poor recent history."""
        from datetime import datetime
        # No history -> Continue (Returns (False, "No recent data"))
        skip, msg = tracker.should_skip_symbol('BTC/USDT')
        assert skip is False
        
        # Bad history (0% WR)
        now_iso = datetime.now().isoformat()
        tracker.trades = [
            {'symbol': 'BAD/USDT', 'result': 'LOSS', 'timestamp': now_iso},
            {'symbol': 'BAD/USDT', 'result': 'LOSS', 'timestamp': now_iso},
            {'symbol': 'BAD/USDT', 'result': 'LOSS', 'timestamp': now_iso}
        ]
        skip_bad, msg_bad = tracker.should_skip_symbol('BAD/USDT', min_wr=0.3, min_trades=3)
        assert skip_bad is True
