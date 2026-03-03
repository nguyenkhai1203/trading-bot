import logging
import time
import pandas as pd
import numpy as np
from typing import Optional, Dict

class BTCAnalyzer:
    """
    BTC Macro Signal (BMS) "Supreme Filter" Analyzer.
    Calculates a composite sentiment score for BTC to guide Altcoin trading.
    """
    def __init__(self, data_manager, db, exchange_adapter=None):
        self.dm = data_manager  # MarketDataManager
        self.db = db            # DataManager (SQLite)
        self.exchange = exchange_adapter
        self.logger = logging.getLogger("BTCAnalyzer")
        
        # Internal Weights (w_n) for BMS base calculation
        self.weights = {
            'trend': 0.4,
            'momentum': 0.3,
            'volatility': 0.2,
            'dominance': 0.1
        }
        
    async def update_sentiment(self, symbol: str = 'BTC/USDT:USDT'):
        """
        Runs Multi-Timeframe (MTF) analysis and persists aggregated BMS to DB.
        Aggregates: 1h (30%), 4h (40%), 1d (30%)
        """
        try:
            results = {}
            for tf, weight in [('1h', 0.3), ('4h', 0.4), ('1d', 0.3)]:
                df = self.dm.get_data_with_features(symbol, tf)
                if df is not None and not df.empty:
                    bms_df = self.calculate_bulk_sentiment(df)
                    if not bms_df.empty:
                        last = bms_df.iloc[-1]
                        results[tf] = {
                            'bms': last['bms'],
                            'trend': last['s_trend'],
                            'momentum': last['s_momentum'],
                            'vol': last['s_vol'],
                            'dom': last['s_dom'],
                            'weight': weight
                        }

            if not results:
                self.logger.warning(f"No data for {symbol} to calculate BMS")
                return None

            # 2. MTF Aggregation
            total_w = sum(r['weight'] for r in results.values())
            m_bms = sum(r['bms'] * r['weight'] for r in results.values()) / total_w
            m_trend = sum(r['trend'] * r['weight'] for r in results.values()) / total_w
            m_momentum = sum(r['momentum'] * r['weight'] for r in results.values()) / total_w
            m_vol = sum(r['vol'] * r['weight'] for r in results.values()) / total_w
            m_dom = sum(r['dom'] * r['weight'] for r in results.values()) / total_w
            
            # 3. Determine Zone based on Aggregated BMS
            m_zone = 'YELLOW'
            if m_bms < 0.3: m_zone = 'RED'
            elif m_bms > 0.7: m_zone = 'GREEN'
            
            # 4. Persist to DB
            await self.db.upsert_market_sentiment(
                symbol=symbol,
                bms=m_bms,
                sentiment_zone=m_zone,
                trend_score=m_trend,
                momentum_score=m_momentum,
                volatility_score=m_vol,
                dominance_score=m_dom
            )
            
            self.logger.info(f"[BMS-MTF] Updated: {m_bms:.2f} ({m_zone}) | T:{m_trend:.2f} M:{m_momentum:.2f} V:{m_vol:.2f}")
            return {'bms': m_bms, 'zone': m_zone, 'trend': m_trend, 'momentum': m_momentum, 'volatility': m_vol, 'dominance': m_dom}
            
        except Exception as e:
            self.logger.error(f"Error updating BTC sentiment: {e}")
            return None

    def calculate_bulk_sentiment(self, df: pd.DataFrame, custom_weights: Optional[Dict] = None) -> pd.DataFrame:
        """Vectorized calculation of BMS for an entire dataframe."""
        if df is None or df.empty: return None
        
        w = custom_weights or self.weights
        df = df.copy()
        
        # 1. Trend Score (Vectorized)
        ema20 = df.get('EMA_21', df.get('EMA_20', df['close']))
        ema50 = df.get('EMA_50', df['close'])
        ema200 = df.get('EMA_200', df['close'])
        
        t_score = pd.Series(0.0, index=df.index)
        t_score += np.where(df['close'] > ema20, 0.3, -0.3)
        t_score += np.where(ema20 > ema50, 0.3, -0.3)
        t_score += np.where(ema50 > ema200, 0.4, -0.4)
        df['s_trend'] = t_score
        
        # 2. Momentum Score (Vectorized)
        rsi = df.get('RSI_14', pd.Series(50, index=df.index))
        macd = df.get('MACD', pd.Series(0, index=df.index))
        macd_sig = df.get('MACD_signal', pd.Series(0, index=df.index))
        
        m_score = pd.Series(0.0, index=df.index)
        m_score += np.where(rsi > 65, 0.5, np.where(rsi > 55, 0.2, 0.0))
        m_score += np.where(rsi < 35, -0.5, np.where(rsi < 45, -0.2, 0.0))
        m_score += np.where(macd > macd_sig, 0.5, -0.5)
        df['s_momentum'] = m_score.clip(-1.0, 1.0)
        
        # 3. Volatility Score (Vectorized)
        atr = df.get('ATR', pd.Series(0, index=df.index))
        vol_ratio = atr / df['close']
        v_score = np.where(vol_ratio > 0.02, -0.8, np.where(vol_ratio > 0.015, -0.4, 0.2))
        df['s_vol'] = v_score
        
        # 4. Dominance Score (Limited vectorized - using change in BTC price as proxy if BTCDOM missing)
        # In bulk mode, we might not have aligned BTCDOM, so we use a rolling change of BTC
        df['s_dom'] = df['close'].pct_change(5).fillna(0).clip(-1.0, 1.0)
        
        # 5. Composite
        df['raw_bms'] = (
            (df['s_trend'] * w['trend']) +
            (df['s_momentum'] * w['momentum']) +
            (df['s_vol'] * w['volatility']) +
            (df['s_dom'] * w['dominance'])
        )
        
        df['bms'] = ((df['raw_bms'] + 1) / 2.0).clip(0.0, 1.0)
        df['zone'] = np.where(df['bms'] < 0.3, 'RED', np.where(df['bms'] > 0.7, 'GREEN', 'YELLOW'))
        
        return df

    def _calculate_trend_score(self, row) -> float:
        """Sn:EMA 20/50/200 Relationship. Returns -1.0 to 1.0."""
        try:
            price = row['close']
            ema20 = row.get('EMA_21', row.get('EMA_20', price)) # Fallback
            ema50 = row.get('EMA_50', price)
            ema200 = row.get('EMA_200', price)
            
            score = 0
            if price > ema20: score += 0.3
            if ema20 > ema50: score += 0.3
            if ema50 > ema200: score += 0.4
            
            if price < ema20: score -= 0.3
            if ema20 < ema50: score -= 0.3
            if ema50 < ema200: score -= 0.4
            
            return score
        except: return 0.0

    def _calculate_momentum_score(self, row) -> float:
        """Sn: RSI & MACD. Returns -1.0 to 1.0."""
        try:
            rsi = row.get('RSI_14', 50)
            macd = row.get('MACD', 0)
            macd_signal = row.get('MACD_signal', 0)
            
            score = 0
            # RSI Contribution
            if rsi > 55: score += 0.2
            if rsi > 65: score += 0.3
            if rsi < 45: score -= 0.2
            if rsi < 35: score -= 0.3
            
            # MACD Contribution
            if macd > macd_signal: score += 0.5
            else: score -= 0.5
            
            return max(-1.0, min(1.0, score))
        except: return 0.0

    def _calculate_volatility_score(self, row) -> float:
        """Sn: ATR relative to price (Panic check). Returns -1.0 to 1.0."""
        try:
            # High ATR relative to price usually means panic/uncertainty
            atr = row.get('ATR', 0)
            price = row['close']
            if price == 0: return 0.0
            
            vol_ratio = atr / price
            # Heuristic: if vol_ratio is 2x its normal state, it's panic
            # We don't have historical mean here yet, so we use a threshold
            # Normal BTC 1h ATR/Price is ~0.005-0.01 (0.5-1%)
            if vol_ratio > 0.02: return -0.8 # Panic
            if vol_ratio > 0.015: return -0.4
            return 0.2 # Stable/Healthy
        except: return 0.0

    async def _calculate_dominance_score(self) -> float:
        """Sn: BTC Dominance (BTCDOM Index). Returns -1.0 to 1.0."""
        try:
            df = self.dm.get_data('BTCDOM/USDT:USDT', '1h')
            if df is not None and not df.empty:
                last_price = df.iloc[-1]['close']
                prev_price = df.iloc[-5]['close'] if len(df) > 5 else last_price
                change = (last_price - prev_price) / prev_price
                return max(-1.0, min(1.0, change * 100)) 
        except: pass
        return 0.0

    def optimize_weights(self, symbol: str = 'BTC/USDT:USDT', timeframe: str = '1h') -> Dict:
        """
        LOOP A: Internal Optimizer. Find best internal w_n for BMS components.
        Objective: Maximize correlation between raw_bms and 24h future returns.
        """
        df = self.dm.get_data_with_features(symbol, timeframe)
        if df is None or len(df) < 200: return self.weights
        
        df = df.copy()
        df['Target'] = df['close'].shift(-24) / df['close'] - 1.0
        df = df.dropna(subset=['Target'])
        
        # Pre-calculate sub-scores (vectorized)
        df = self.calculate_bulk_sentiment(df)
        
        combos = [
            [0.4, 0.3, 0.2, 0.1], # Default
            [0.5, 0.2, 0.2, 0.1],
            [0.3, 0.4, 0.2, 0.1],
            [0.5, 0.3, 0.1, 0.1],
            [0.4, 0.4, 0.1, 0.1],
            [0.6, 0.2, 0.1, 0.1],
            [0.2, 0.2, 0.3, 0.3], # Defensive
        ]
        
        best_w = self.weights
        max_corr = -1.0
        
        for c in combos:
            w_test = {'trend': c[0], 'momentum': c[1], 'volatility': c[2], 'dominance': c[3]}
            raw = (
                (df['s_trend'] * w_test['trend']) +
                (df['s_momentum'] * w_test['momentum']) +
                (df['s_vol'] * w_test['volatility']) +
                (df['s_dom'] * w_test['dominance'])
            )
            corr = raw.corr(df['Target'])
            if corr > max_corr:
                max_corr = corr
                best_w = w_test
                
        self.logger.info(f"[BMS] Optimized weights (Loop A): {best_w} | Correlation: {max_corr:.4f}")
        self.weights = best_w
        return best_w
