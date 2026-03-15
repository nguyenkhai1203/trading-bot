from typing import Dict, Any, Optional
import logging
from src.infrastructure.repository.database import DataManager as DBManager
from src.data_manager import MarketDataManager
from src.infrastructure.repository.sqlite_trade_repository import SQLiteTradeRepository
from src.infrastructure.repository.sqlite_profile_repository import SQLiteProfileRepository
from src.infrastructure.repository.sqlite_sentiment_repository import SQLiteSentimentRepository
from src.domain.services.risk_service import RiskService
from src.domain.services.strategy_service import StrategyService
from src.application.use_cases.evaluate_strategy import EvaluateStrategyUseCase
from src.application.use_cases.monitor_positions import MonitorPositionsUseCase
from src.application.use_cases.execute_trade import ExecuteTradeUseCase
from src.application.trading.account_sync_service import AccountSyncService
from src.domain.services.notification_service import NotificationService
from src.infrastructure.adapters.exchange_factory import get_active_exchanges_map, create_adapter_from_profile
from src.cooldown_manager import CooldownManager
import asyncio

from src.application.use_cases.manage_position import ManagePositionUseCase

class Container:
    """
    Simple Dependency Injection Container.
    Manages the lifecycle of infrastructure adapters and domain services.
    """
    _instance = None

    def __init__(self, env: str = 'LIVE'):
        self.env = env
        self.logger = logging.getLogger("Container")
        self.db_manager = None
        self.data_manager = None
        self.trade_repo = None
        self.profile_repository = None
        self.sentiment_repository = None
        self.risk_service = None
        self.strategy_service = None
        self.notification_service = None
        self.cooldown_manager = None
        self.adapters = {}
        self.adapters_by_profile = {}
        self.sync_service = None
        self.evaluate_strategy_use_case = None
        self.monitor_positions_use_case = None
        self.execute_trade_use_case = None
        self.manage_position_use_case = None
        self.adapters = None
        self._symbol_locks: Dict[str, asyncio.Lock] = {}
        self.initialized = False

    @classmethod
    async def get_instance(cls, env: str = 'LIVE') -> 'Container':
        if cls._instance is None:
            cls._instance = cls(env)
            await cls._instance.initialize()
        return cls._instance

    def get_symbol_lock(self, symbol: str) -> asyncio.Lock:
        if symbol not in self._symbol_locks:
            self._symbol_locks[symbol] = asyncio.Lock()
        return self._symbol_locks[symbol]

    async def initialize(self):
        """Perform async initialization of repositories and services."""
        if self.initialized:
            return

        self.logger.info("🚀 Initializing System Container...")
        
        # 1. Initialize Infrastructure (Database)
        self.db_manager = await DBManager.get_instance(self.env)
        
        # 2. Initialize Repositories
        self.trade_repo = SQLiteTradeRepository(self.db_manager)
        self.profile_repository = SQLiteProfileRepository(self.db_manager)
        self.sentiment_repository = SQLiteSentimentRepository(self.db_manager)
        
        # 3. Load Active Profiles First to Determine Needed Adapters
        all_profiles = await self.profile_repository.get_active_profiles()
        profiles = [p for p in all_profiles if p['id'] != 0]
        active_exchanges = set(p['exchange'].upper() for p in profiles)

        # 4. Infrastructure Adapters (Filtered)
        full_adapters_map = get_active_exchanges_map()
        # Keep only adapters for exchanges that have active profiles
        self.adapters = {name: adapter for name, adapter in full_adapters_map.items() if name in active_exchanges}
        
        self.data_manager = MarketDataManager(self.db_manager, adapters=self.adapters)
        
        # 5. Initialize Domain Services
        import logging
        logger = logging.getLogger("CooldownManager")
        self.cooldown_manager = CooldownManager(self.db_manager, logger, self.env)
        self.data_manager.set_cooldown_manager(self.cooldown_manager)
        
        self.risk_service = RiskService()
        self.strategy_service = StrategyService()
        self.notification_service = NotificationService()
        
        # 6. Initialize profile-specific adapters for trading
        for profile in profiles:
            try:
                adapter = await create_adapter_from_profile(profile)
                if adapter:
                    self.adapters_by_profile[profile['id']] = adapter
                    self.logger.info(f"Initialized profile-specific adapter for Profile {profile['id']} ({profile['name']})")
            except Exception as e:
                self.logger.error(f"Failed to initialize adapter for Profile {profile['id']}: {e}")

        self.sync_service = AccountSyncService(profiles, self.adapters_by_profile)
        
        # Priority: Link active symbols to DataManager for prioritized OHLCV fetches
        self.data_manager.set_active_symbols_provider(self.sync_service.get_active_symbols)
        
        # Hydrate cooldowns for all profiles
        for p in profiles:
            await self.cooldown_manager.sync_from_db(p['id'])
            
        # 7. Use Cases
        self.evaluate_strategy_use_case = EvaluateStrategyUseCase(self.strategy_service, self.data_manager, self.cooldown_manager)
        self.monitor_positions_use_case = MonitorPositionsUseCase(
            self.sync_service, 
            self.trade_repo, 
            self.risk_service, 
            self.notification_service,
            self.cooldown_manager,
            self.evaluate_strategy_use_case
        )
        
        self.execute_trade_use_case = ExecuteTradeUseCase(
            self.trade_repo, 
            self.adapters, 
            self.risk_service, 
            self.notification_service,
            self.cooldown_manager,
            self.sync_service,
            self
        )

        self.manage_position_use_case = ManagePositionUseCase(
            self.trade_repo,
            self.adapters,
            self.notification_service
        )
        
        # 6.5. Sync strategy config cache from DB
        try:
            from src.strategy import WeightedScoringStrategy
            all_configs = await self.db_manager.get_all_strategy_configs()
            WeightedScoringStrategy.update_cache(all_configs)
            self.logger.info(f"📥 Synced {len(all_configs)} strategy configurations from DB.")
        except Exception as e:
            self.logger.error(f"Failed to sync strategy cache: {e}")

        # 6.6. Sync exchange time for all adapters (Critical for Windows)
        time_sync_tasks = []
        for adapter in self.adapters_by_profile.values():
            time_sync_tasks.append(adapter.sync_time())
        if time_sync_tasks:
            await asyncio.gather(*time_sync_tasks, return_exceptions=True)
            self.logger.info("⏱️ Synchronized exchange time for all active adapters.")

        self.initialized = True

    async def close(self):
        if self.db_manager:
            await self.db_manager.close()
        if self.data_manager:
            await self.data_manager.close()
        self.initialized = False
