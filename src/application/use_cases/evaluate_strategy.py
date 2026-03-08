from typing import Dict, Any, Optional
import logging
from src.domain.services.strategy_service import StrategyService
from src import config

class EvaluateStrategyUseCase:
    """
    Use Case: Evaluate a trading strategy for a symbol and timeframe.
    """
    def __init__(self, strategy_service: StrategyService, data_manager: Any):
        self.strategy_service = strategy_service
        self.data_manager = data_manager
        self.logger = logging.getLogger("EvaluateStrategyUseCase")

    async def execute(self, symbol: str, timeframe: str, exchange: str) -> Optional[Dict[str, Any]]:
        """
        Runs the evaluation logic.
        """
        # 1. Fetch feature data
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
            
        signal['last_row'] = last_row
        return signal
