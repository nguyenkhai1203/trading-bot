from typing import Dict, Any, Optional
# Removed circular import to src.strategy
import logging

class StrategyService:
    """
    Domain Service for strategy evaluation and risk parameter calculation.
    Wraps legacy WeightedScoringStrategy into the new architecture.
    """
    def __init__(self):
        self.logger = logging.getLogger("StrategyService")

    def get_signal(self, symbol: str, timeframe: str, market_row: Any, exchange: str = 'BINANCE') -> Dict[str, Any]:
        """
        Evaluate strategy and return a signal.
        """
        # 1. Initialize/Get strategy instance
        from src.strategy import WeightedScoringStrategy
        strategy = WeightedScoringStrategy(symbol=symbol, timeframe=timeframe, exchange=exchange)
        
        # 2. Extract BMS info (expected by strategy)
        bms_score = float(market_row.get('bms_score') or 0.5)
        bms_zone = str(market_row.get('bms_zone') or 'NEUTRAL')
        
        # 3. Call strategy
        signal = strategy.get_signal(
            market_row, 
            tracker=None, 
            bms_score=bms_score, 
            bms_zone=bms_zone
        )
        
        # 4. Enrich with metadata & dynamic risk params
        sl_pct, tp_pct = strategy.get_dynamic_risk_params(market_row)
        signal.update({
            'symbol': symbol,
            'timeframe': timeframe,
            'sl_pct': sl_pct,
            'tp_pct': tp_pct
        })
        
        return signal

    @staticmethod
    def calculate_weighted_score(row, weights, long_signals, short_signals):
        """
        Base heuristic scoring logic.
        """
        score_long = 0.0
        score_short = 0.0
        reasons_long = []
        reasons_short = []
        
        for sig in long_signals:
            # Check for column with signal_ prefix
            if row.get(f"signal_{sig}"):
                score_long += weights.get(sig, 0)
                reasons_long.append(sig)
        
        for sig in short_signals:
            if row.get(f"signal_{sig}"):
                score_short += weights.get(sig, 0)
                reasons_short.append(sig)
                
        return score_long, score_short, reasons_long, reasons_short

    @staticmethod
    def apply_bms_weighting(score_long, score_short, bms_score, bms_zone, w_btc, w_alt):
        """
        Adjust scores based on BTC Market Structure (BMS).
        """
        # Apply 1.2x BTC weight boost ONLY in GREEN zone as per test expectations
        if bms_zone == 'GREEN':
            w_btc_adj = min(0.95, w_btc * 1.2)
            w_alt_adj = 1.0 - w_btc_adj
        else:
            w_btc_adj = w_btc
            w_alt_adj = w_alt

        if bms_zone == 'RED':
            # Soften veto: Allow LONG if altcoin signal is very strong (> 0.8)
            if score_long < 0.8:
                score_long = 0.0
            else:
                score_long *= 0.8 # Small penalty for counter-trend
                
            btc_short_bias = (1.0 - bms_score) * 10.0
            score_short = (score_short * w_alt_adj) + (btc_short_bias * w_btc_adj)
        elif bms_zone == 'GREEN':
            # Soften veto: Allow SHORT if altcoin signal is very strong (> 0.8)
            if score_short < 0.8:
                score_short = 0.0
            else:
                score_short *= 0.8 # Small penalty for counter-trend
                
            btc_long_bias = bms_score * 10.0
            score_long = (score_long * w_alt_adj) + (btc_long_bias * w_btc_adj)
        else:
            btc_long_bias = bms_score * 10.0
            btc_short_bias = (1.0 - bms_score) * 10.0
            score_long = (score_long * w_alt_adj) + (btc_long_bias * w_btc_adj)
            score_short = (score_short * w_alt_adj) + (btc_short_bias * w_btc_adj)
            
        return score_long, score_short
