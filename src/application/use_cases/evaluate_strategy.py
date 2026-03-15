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
        if not self._is_data_fresh(df, timeframe, symbol):
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
    def _is_data_fresh(self, df: pd.DataFrame, timeframe: str, symbol: str) -> bool:
        """Helper to verify if the latest candle in DataFrame is recent enough."""
        try:
            import time
            import numpy as np
            raw_ts = df.iloc[-1]['timestamp']
            
            if isinstance(raw_ts, pd.Timestamp):
                last_ts = int(raw_ts.timestamp() * 1000)
            elif isinstance(raw_ts, np.datetime64):
                last_ts = int(raw_ts.astype('datetime64[ms]').astype(int))
            else:
                last_ts = int(float(raw_ts) * 1000) if float(raw_ts) < 1e11 else int(raw_ts)
                
            now_ts = int(time.time() * 1000)
            
            # Calculate max allowable age (allow 3 candles lag)
            tf_ms = self._get_timeframe_ms(timeframe)
            
            if (now_ts - last_ts) > (tf_ms * 3):
                age_mins = int((now_ts - last_ts) / 60000)
                self.logger.warning(f"[{symbol}:{timeframe}] STALE DATA DETECTED ({age_mins}m old). Skipping evaluation.")
                return False
            return True
        except Exception as e:
            self.logger.error(f"Freshness check error for {symbol}: {e}")
            return False

    def _get_timeframe_ms(self, timeframe: str) -> int:
        """Convert timeframe string to milliseconds."""
        tf_str = str(timeframe)
        if tf_str.endswith('m'): return int(tf_str[:-1]) * 60000
        if tf_str.endswith('h'): return int(tf_str[:-1]) * 3600000
        if tf_str.endswith('d'): return int(tf_str[:-1]) * 86400000
        return 3600000 # Default 1h
