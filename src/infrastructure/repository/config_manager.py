# -*- coding: utf-8 -*-
"""
ConfigManager Utility
Centralized management for strategy configurations, bridging DB and memory.
"""
import os
import json
import logging
import asyncio
from typing import Optional, List, Dict, Any
from .database import DataManager

class ConfigManager:
    _instance = None
    _lock = asyncio.Lock()

    def __init__(self, db: DataManager):
        self.db = db
        self.logger = logging.getLogger("ConfigManager")

    @classmethod
    async def get_instance(cls, env: str = 'LIVE') -> 'ConfigManager':
        async with cls._lock:
            if cls._instance is None:
                db = await DataManager.get_instance(env)
                cls._instance = cls(db)
            return cls._instance

    async def get_config(self, symbol: str, timeframe: str, exchange: str) -> Optional[dict]:
        """Fetch config from DB."""
        return await self.db.get_strategy_config(symbol, timeframe, exchange)

    async def save_config(self, symbol: str, timeframe: str, exchange: str, config_dict: dict):
        """Save config to DB and update Strategy cache."""
        await self.db.save_strategy_config(symbol, timeframe, exchange, config_dict)
        
        # Update Strategy cache if running in bot context
        try:
            # Delayed import to avoid circular dependency
            from ...strategy import WeightedScoringStrategy
            all_configs = await self.db.get_all_strategy_configs()
            WeightedScoringStrategy.update_cache(all_configs)
        except ImportError:
            pass

    async def get_all_configs(self, exchange: Optional[str] = None) -> List[dict]:
        """Fetch all configs."""
        return await self.db.get_all_strategy_configs(exchange)

    async def migrate_from_json(self, json_path: str, exchange_fallback: str = 'BINANCE'):
        """
        One-time migration from strategy_config.json to SQLite.
        Expects legacy format: {"EXCHANGE_SYMBOL_TF": { ...weights, performance, etc... }}
        """
        if not os.path.exists(json_path):
            self.logger.warning(f"Migration source {json_path} not found.")
            return

        try:
            with open(json_path, 'r') as f:
                legacy_data = json.load(f)
            
            migrated = 0
            for key, config in legacy_data.items():
                # Parse key: EXCHANGE_SYMBOL_TF or SYMBOL_TF
                parts = key.split('_')
                if len(parts) >= 3:
                    ex = parts[0]
                    tf = parts[-1]
                    sym = "_".join(parts[1:-1])
                elif len(parts) == 2:
                    ex = exchange_fallback
                    sym, tf = parts
                else:
                    continue

                # Normalize symbol (ETH_USDT -> ETH/USDT)
                norm_sym = sym.replace('_', '/')
                if 'USDT' in norm_sym and '/' not in norm_sym:
                    norm_sym = norm_sym.replace('USDT', '/USDT')

                await self.save_config(norm_sym, tf, ex, config)
                migrated += 1
            
            self.logger.info(f"✅ Migrated {migrated} configurations from {json_path} to DB.")
            # Optional: rename json_path to .bak
            os.rename(json_path, json_path + ".bak")
            
        except Exception as e:
            self.logger.error(f"Migration failed: {e}")

async def run_migration():
    """CLI helper for migration."""
    import sys
    env = 'LIVE'
    if '--test' in sys.argv or '--dry-run' in sys.argv:
        env = 'TEST'
    
    mgr = await ConfigManager.get_instance(env)
    # Root is 3 levels up from src/infrastructure/repository/config_manager.py
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    json_path = os.path.join(project_root, 'src', 'strategy_config.json')
    
    await mgr.migrate_from_json(json_path)

if __name__ == "__main__":
    asyncio.run(run_migration())
