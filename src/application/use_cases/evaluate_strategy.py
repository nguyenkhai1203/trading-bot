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

    async def execute(self, symbol: str, timeframe: str, exchange: str, profile_id: int = 0) -> Optional[Dict[str, Any]]:
        """
        Runs the evaluation logic.
        """
        # 1. SL Cooldown Check
        if self.cooldown_manager and self.cooldown_manager.is_in_cooldown(exchange, symbol, profile_id):
            return None

        # 2. Fetch feature data
        df = self.data_manager.get_data_with_features(symbol, timeframe, exchange=exchange)
        if df is None or df.empty:
            return None

        # 3. Data Freshness Guard
        import time
        import pandas as pd
        import numpy as np

        # Determine last candle timestamp
        try:
            raw_ts = df.iloc[-1]['timestamp']
            last_ts = 0
            
            # Robust conversion to milliseconds
            if isinstance(raw_ts, pd.Timestamp):
                last_ts = int(raw_ts.timestamp() * 1000)
            elif isinstance(raw_ts, np.datetime64):
                # Convert ns to ms
                last_ts = int(raw_ts.astype('datetime64[ms]').astype(int))
            elif hasattr(raw_ts, 'timestamp'):
                last_ts = int(raw_ts.timestamp() * 1000)
            else:
                try:
                    val = float(raw_ts)
                    # If it's in seconds (e.g. 1.7e9), convert to ms
                    last_ts = int(val * 1000) if val < 1e11 else int(val)
                except:
                    last_ts = 0
        except Exception as e:
            self.logger.error(f"Error extracting timestamp: {e}")
            last_ts = 0
        
        now_ts = int(time.time() * 1000)
        
        # Calculate max allowable age based on timeframe (allow 3 candles lag)
        tf_ms = 3600000 # Default 1h
        if str(timeframe).endswith('m'): tf_ms = int(str(timeframe)[:-1]) * 60000
        elif str(timeframe).endswith('h'): tf_ms = int(str(timeframe)[:-1]) * 3600000
        elif str(timeframe).endswith('d'): tf_ms = int(str(timeframe)[:-1]) * 86400000
        
        if (now_ts - last_ts) > (tf_ms * 3):
            age_mins = int((now_ts - last_ts) / 60000)
            self.logger.warning(
                f"[{symbol}:{timeframe}] STALE DATA DETECTED ({age_mins}m old). "
                f"last_ts={last_ts}, now_ts={now_ts}, raw_ts={raw_ts} (type:{type(raw_ts)}). Skipping evaluation."
            )
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
