from typing import Dict, Any, Optional
from src.domain.repository import ISentimentRepository
from .database import DataManager

class SQLiteSentimentRepository(ISentimentRepository):
    def __init__(self, data_manager: DataManager):
        self.dm = data_manager

    async def upsert_sentiment(self, symbol: str, data: Dict[str, Any]) -> None:
        await self.dm.upsert_market_sentiment(
            symbol=symbol,
            bms=data.get('bms', 0),
            sentiment_zone=data.get('sentiment_zone', 'YELLOW'),
            trend_score=data.get('trend_score', 0),
            momentum_score=data.get('momentum_score', 0),
            volatility_score=data.get('volatility_score', 0),
            dominance_score=data.get('dominance_score', 0)
        )

    async def get_latest_sentiment(self, symbol: str) -> Optional[Dict[str, Any]]:
        return await self.dm.get_latest_market_sentiment(symbol)
