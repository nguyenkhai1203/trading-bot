from typing import Dict, Any, Optional
import logging
import pandas as pd
from src.domain.services.strategy_service import StrategyService
from src import config

class EvaluateStrategyUseCase:
    """
    Use Case: Evaluate a trading strategy for a symbol and timeframe.
    """
    def __init__(self, strategy_service: StrategyService, data_manager: Any, cooldown_manager: Any = None):
        self.strategy_service = strategy_service
        self.data_manager = data_manager
        self.cooldown_manager = cooldown_manager
        self.logger = logging.getLogger("EvaluateStrategyUseCase")

    async def execute(self, symbol: str, timeframe: str, exchange: str) -> Optional[Dict[str, Any]]:
        """
        Runs the evaluation logic.
        """
        # 1. SL Cooldown Check
        if self.cooldown_manager and self.cooldown_manager.is_in_cooldown(exchange, symbol):
            return None

        # 2. Fetch feature data
        df = self.data_manager.get_data_with_features(symbol, timeframe, exchange=exchange)
        if df is None or df.empty:
            return None

        # 2. Extract last row
        last_row = df.iloc[-1]
        
        # 3. Get signal from Domain Service
        # Note: We'll eventually move the actual Strategy class logic into StrategyService
        # For now, we interface with the existing Strategy classes (via Domain Service wrapper)
        signal = self.strategy_service.get_signal(symbol, timeframe, last_row, exchange=exchange)
        
        if not signal or signal.get('side') == 'SKIP':
            return None
            
        conf = signal.get('confidence')
        if conf is None or conf < config.MIN_CONFIDENCE_TO_TRADE:
            return None
            
        # 4. Enrich signal with Technical Levels for Limit Entry
        for level in ['support_level', 'resistance_level', 'fibo_618', 'fibo_50', 'fibo_382', 'EMA_21', 'EMA_50', 'EMA_200']:
            if level in df.columns:
                signal[level] = float(last_row[level]) if not pd.isna(last_row[level]) else None

        signal['last_row_summary'] = last_row.to_dict()
        return signal
