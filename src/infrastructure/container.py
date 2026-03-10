from typing import Dict, Any, Optional
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
from src.infrastructure.adapters.exchange_factory import get_active_exchanges_map
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
        self.db_manager = None
        self.data_manager = None
        self.trade_repo = None
        self.profile_repository = None
        self.sentiment_repository = None
        self.risk_service = None
        self.strategy_service = None
        self.notification_service = None
        self.cooldown_manager = None
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
        if self.initialized:
            return

        # 1. Initialize Adapters first (some services need them)
        self.adapters = get_active_exchanges_map()

        # 2. Initialize Infrastructure (Database & Market Data)
        self.db_manager = await DBManager.get_instance(self.env)
        self.data_manager = MarketDataManager(self.db_manager, adapters=self.adapters)
        
        # 3. Initialize Repositories
        self.trade_repo = SQLiteTradeRepository(self.db_manager)
        self.profile_repository = SQLiteProfileRepository(self.db_manager)
        self.sentiment_repository = SQLiteSentimentRepository(self.db_manager)
        
        # 4. Initialize Domain Services (Inject adapters map into CooldownManager)
        import logging
        logger = logging.getLogger("CooldownManager")
        self.cooldown_manager = CooldownManager(self.db_manager, logger, self.env)
        self.data_manager.set_cooldown_manager(self.cooldown_manager)
        
        self.risk_service = RiskService()
        self.strategy_service = StrategyService()
        self.notification_service = NotificationService()
        
        # 5. Initialize Application Services & Use Cases
        profiles = await self.profile_repository.get_active_profiles()
        self.sync_service = AccountSyncService(profiles, self.adapters)
        
        # Priority: Link active symbols to DataManager for prioritized OHLCV fetches
        self.data_manager.set_active_symbols_provider(self.sync_service.get_active_symbols)
        
        # Hydrate cooldowns for all profiles
        for p in profiles:
            await self.cooldown_manager.sync_from_db(p['id'])
            
        self.evaluate_strategy_use_case = EvaluateStrategyUseCase(self.strategy_service, self.data_manager, self.cooldown_manager)
        self.monitor_positions_use_case = MonitorPositionsUseCase(
            self.sync_service, 
            self.trade_repo, 
            self.risk_service, 
            self.notification_service,
            self.cooldown_manager
        )
        
        self.execute_trade_use_case = ExecuteTradeUseCase(
            self.trade_repo, 
            self.adapters, 
            self.risk_service, 
            self.notification_service,
            self.cooldown_manager,
            self.sync_service
        )

        self.manage_position_use_case = ManagePositionUseCase(
            self.trade_repo,
            self.adapters,
            self.notification_service
        )
        
        self.initialized = True

    async def close(self):
        if self.db_manager:
            await self.db_manager.close()
        if self.data_manager:
            await self.data_manager.close()
        self.initialized = False
