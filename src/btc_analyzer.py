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
        
        # Anti-spam state
        self._last_sent_zone = None
        self._last_sent_score = 0.0
        self._last_msg_time = 0
        
    async def update_sentiment(self, symbol: str = 'BTC/USDT:USDT'):
        """
        Runs Multi-Timeframe (MTF) analysis and persists aggregated BMS to DB.
        """
        try:
            from src.config import BMS_CONFIG
            mtf_weights = BMS_CONFIG.get('MTF_WEIGHTS', {'1h': 0.3, '4h': 0.4, '1d': 0.3})
            
            results = {}
            # We also need BTCDOM data for the dominance score to work in calculation
            # In live update, we fetch it via DM
            dom_df = self.dm.get_data('BTCDOM/USDT:USDT', '1h')
            
            for tf, weight in mtf_weights.items():
                df = self.dm.get_data_with_features(symbol, tf)
                if df is not None and not df.empty:
                    # Merge BTCDOM into BTC df for the calculation if timeframe is 1h
                    if tf == '1h' and dom_df is not None:
                        dom_subset = dom_df[['timestamp', 'close']].rename(columns={'close': 'BTCDOM_close'})
                        df = pd.merge_asof(df.sort_values('timestamp'), dom_subset.sort_values('timestamp'), on='timestamp', direction='backward')
                    
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
            
            # 3. Determine Zone based on Aggregated BMS and Config
            veto_strong = BMS_CONFIG.get('VETO_THRESHOLD_STRONG', 0.70)
            m_zone = 'YELLOW'
            if m_bms < (1 - veto_strong): m_zone = 'RED'
            elif m_bms > veto_strong: m_zone = 'GREEN'
            
            from src.infrastructure.notifications.notification import format_bms_report, send_telegram_chunked
            
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
            
            # 5. Notify via Telegram (with Spam Filter)
            now = time.time()
            score_diff = abs(m_bms - self._last_sent_score)
            zone_changed = m_zone != self._last_sent_zone
            time_passed = now - self._last_msg_time > 3600 # 1 hour
            
            if zone_changed or score_diff > 0.05 or time_passed:
                bms_report = format_bms_report({
                    'bms': m_bms,
                    'sentiment_zone': m_zone,
                    'trend_score': m_trend,
                    'momentum_score': m_momentum,
                    'volatility_score': m_vol,
                    'dominance_score': m_dom
                })
                await send_telegram_chunked(bms_report)
                
                # Update anti-spam state
                self._last_sent_zone = m_zone
                self._last_sent_score = m_bms
                self._last_msg_time = now
            else:
                self.logger.debug(f"[BMS] Skipping Telegram report (Score diff: {score_diff:.3f}, Zone same)")
            
            self.logger.info(f"[BMS-MTF] Updated: {m_bms:.2f} ({m_zone}) | T:{m_trend:.2f} M:{m_momentum:.2f} V:{m_vol:.2f} D:{m_dom:.2f}")
            return {'bms': m_bms, 'zone': m_zone, 'trend': m_trend, 'momentum': m_momentum, 'volatility': m_vol, 'dominance': m_dom}
            
        except Exception as e:
            self.logger.error(f"Error updating BTC sentiment: {e}")
            return None

    def calculate_bulk_sentiment(self, df: pd.DataFrame, custom_weights: Optional[Dict] = None) -> pd.DataFrame:
        """Vectorized calculation of BMS v2.0 for an entire dataframe."""
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
        
        # 3. Adaptive Volatility Score (Vectorized)
        atr = df.get('ATR', df.get('ATR_14', pd.Series(0, index=df.index)))
        # Use config for window if available
        from src.config import BMS_CONFIG
        v_window = BMS_CONFIG.get('VOLATILITY_WINDOW', 200)
        
        # Normal volatility baseline (rolling median)
        v_baseline = atr.rolling(v_window).median().fillna(atr)
        vol_ratio = atr / v_baseline.replace(0, 1)
        
        # v_penalty: 1.0 (normal) -> -1.0 (extreme spike)
        # vol_ratio > 2.0 (2x normal) -> -1.0 penalty
        v_score = np.where(vol_ratio > 2.5, -1.0, 
                  np.where(vol_ratio > 1.8, -0.6,
                  np.where(vol_ratio > 1.3, -0.2, 0.2)))
        
        # Boost volatility score if price is moving WITH trend and low volume spike
        # (Healthy organic move vs panic spike)
        v_score = np.where((t_score > 0) & (df['close'] > df['close'].shift(1)) & (vol_ratio < 1.5), v_score + 0.1, v_score)
        df['s_vol'] = pd.Series(v_score, index=df.index).clip(-1.0, 0.5)
        
        # 4. Dominance Score (v2.0: using real BTCDOM if available)
        # Attempt to find BTCDOM related columns if merged, otherwise fallback
        dom_col = [c for c in df.columns if 'BTCDOM' in c.upper() and 'close' in c.lower()]
        if dom_col:
            dom_price = df[dom_col[0]]
            d_window = BMS_CONFIG.get('DOMINANCE_WINDOW', 50)
            dom_pct_change = dom_price.pct_change(5)
            # Z-score of changes
            dom_mean = dom_pct_change.rolling(d_window).mean()
            dom_std = dom_pct_change.rolling(d_window).std().replace(0, 0.001)
            dom_z = (dom_pct_change - dom_mean) / dom_std
            df['s_dom'] = (dom_z / 3.0).clip(-1.0, 1.0) # Scaled to -1 to 1
        else:
            # Fallback: BTC price pct change but with Z-score for adaptivity
            btc_pct = df['close'].pct_change(5).fillna(0)
            btc_mean = btc_pct.rolling(50).mean()
            btc_std = btc_pct.rolling(50).std().replace(0, 0.001)
            btc_z = (btc_pct - btc_mean) / btc_std
            df['s_dom'] = (btc_z / 3.0).clip(-1.0, 1.0)
        
        # 5. Composite with INTERACTION Logic
        # Synergy: trend + momentum
        synergy = np.where((df['s_trend'] * df['s_momentum'] > 0), 0.1, 0.0)
        
        df['raw_bms'] = (
            (df['s_trend'] * w['trend']) +
            (df['s_momentum'] * w['momentum']) +
            (df['s_vol'] * w['volatility']) +
            (df['s_dom'] * w['dominance']) +
            synergy
        ).clip(-1.0, 1.0)
        
        df['bms'] = ((df['raw_bms'] + 1) / 2.0).clip(0.0, 1.0)
        
        # Zones based on new thresholds
        veto_strong = BMS_CONFIG.get('VETO_THRESHOLD_STRONG', 0.70)
        df['zone'] = np.where(df['bms'] < (1 - veto_strong), 'RED', 
                     np.where(df['bms'] > veto_strong, 'GREEN', 'YELLOW'))
        
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

    def optimize_weights(self, symbol: str = 'BTC/USDT:USDT') -> Dict:
        """
        LOOP A: Internal Optimizer. Find best internal w_n and MTF weights.
        """
        # 1. Optimize Internal Component Weights
        df_1h = self.dm.get_data_with_features(symbol, '1h')
        if df_1h is None or len(df_1h) < 200: return self.weights
        
        df = df_1h.copy()
        df['Target'] = df['close'].shift(-24) / df['close'] - 1.0
        df = df.dropna(subset=['Target'])
        
        # Pre-calculate sub-scores
        df = self.calculate_bulk_sentiment(df)
        
        combos = [
            [0.4, 0.3, 0.2, 0.1], # Default
            [0.5, 0.2, 0.2, 0.1],
            [0.3, 0.4, 0.2, 0.1],
            [0.5, 0.3, 0.1, 0.1],
            [0.4, 0.4, 0.1, 0.1],
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
                
        self.logger.info(f"[BMS] Optimized component weights: {best_w} | Corr: {max_corr:.4f}")
        self.weights = best_w

        # 2. Optimize MTF Weights
        # Simple heuristic: weight by correlation of each TF's BMS to its 24h future return
        mtf_results = {}
        for tf in ['1h', '4h', '1d']:
            tf_data = self.dm.get_data_with_features(symbol, tf)
            if tf_data is not None and len(tf_data) > 100:
                tf_data['Target'] = tf_data['close'].shift(-24) / tf_data['close'] - 1.0
                bms_df = self.calculate_bulk_sentiment(tf_data)
                corr = bms_df['bms'].corr(tf_data['Target'])
                mtf_results[tf] = max(0.1, corr) if not pd.isna(corr) else 0.3
            else:
                mtf_results[tf] = 0.3
        
        # Normalize MTF weights
        total = sum(mtf_results.values())
        final_mtf = {k: round(v/total, 2) for k, v in mtf_results.items()}
        self.logger.info(f"[BMS] Optimized MTF weights: {final_mtf}")
        
        return {'components': best_w, 'mtf': final_mtf}
