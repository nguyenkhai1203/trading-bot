import pytest
import pandas as pd
import numpy as np
from src.strategy import WeightedScoringStrategy

def test_strategy_bms_weighting():
    # Setup strategy
    strategy = WeightedScoringStrategy(symbol="ETH/USDT", timeframe="1h")
    strategy.w_btc = 0.6
    strategy.w_alt = 0.4
    
    # Mock row data (Bullish Alt signal Sum = 5.0)
    row = {
        'close': 2000,
        'signal_EMA_9_cross_21_up': True, # 1.5
        'signal_EMA_50_gt_200': True,      # 2.0
        'signal_MACD_cross_up': True,      # 1.5
    }
    
    # Case 1: Neutral BMS (0.5)
    sig1 = strategy.get_signal(row, use_brain=False, bms_score=0.5, bms_zone='YELLOW')
    # Long: (5.0 * 0.4) + (0.5 * 10 * 0.6) = 2.0 + 3.0 = 5.0
    assert sig1['side'] == 'BUY'
    assert abs(sig1['confidence'] - 0.5) < 0.01

    # Case 2: Bullish BMS (0.9) - Boosts Long
    sig2 = strategy.get_signal(row, use_brain=False, bms_score=0.9, bms_zone='GREEN')
    # w_btc_adj = 0.6 * 1.2 = 0.72, w_alt_adj = 0.28
    # Long: (5.0 * 0.28) + (0.9 * 10 * 0.72) = 1.4 + 6.48 = 7.88
    assert sig2['side'] == 'BUY'
    assert abs(sig2['confidence'] - 0.788) < 0.01

    # Case 3: Bearish BMS (0.1) - RED Zone Veto Long
    sig3 = strategy.get_signal(row, use_brain=False, bms_score=0.1, bms_zone='RED')
    # Score Long is Vetoed to 0.0
    # Score Short: (0.0 * 0.4) + ((1.0 - 0.1) * 10 * 0.6) = 0 + 5.4 = 5.4
    assert sig3['side'] == 'SELL'
    assert abs(sig3['confidence'] - 0.54) < 0.01

def test_strategy_short_bms():
    strategy = WeightedScoringStrategy(symbol="ETH/USDT", timeframe="1h")
    strategy.w_btc = 0.6
    strategy.w_alt = 0.4
    
    # Bearish Alt signal (Sum = 5.0)
    row = {
        'close': 2000,
        'signal_EMA_9_cross_21_down': True, # 1.5
        'signal_EMA_50_lt_200': True,       # 2.0
        'signal_MACD_cross_down': True,     # 1.5
    }
    
    # Neutral BMS (0.5)
    sig1 = strategy.get_signal(row, use_brain=False, bms_score=0.5, bms_zone='YELLOW')
    # ShortScore: (5.0 * 0.4) + ((1.0-0.5) * 10 * 0.6) = 2.0 + 3.0 = 5.0
    assert sig1['side'] == 'SELL'
    assert abs(sig1['confidence'] - 0.5) < 0.01
    
    # Very Bearish BTC (BMS = 0.1) in RED Zone
    sig2 = strategy.get_signal(row, use_brain=False, bms_score=0.1, bms_zone='RED')
    # ShortScore: (5.0 * 0.4) + ((1.0-0.1) * 10 * 0.6) = 2.0 + 5.4 = 7.4
    assert sig2['side'] == 'SELL'
    assert abs(sig2['confidence'] - 0.74) < 0.01

    # Case 4: Strong Bullish BMS (0.9) - GREEN Zone Veto Short
    sig4 = strategy.get_signal(row, use_brain=False, bms_score=0.9, bms_zone='GREEN')
    # Score Short is Vetoed to 0.0
    # Score Long: (0.0 * 0.28) + (0.9 * 10 * 0.72) = 6.48
    assert sig4['side'] == 'BUY'
    assert abs(sig4['confidence'] - 0.648) < 0.01
