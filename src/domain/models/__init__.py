from datetime import datetime
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field

class Order(BaseModel):
    id: str
    symbol: str
    side: str
    type: str
    qty: Optional[float] = 0.0
    price: Optional[float] = None
    status: str = 'OPENED'
    timestamp: int = Field(default_factory=lambda: int(time.time() * 1000))
    client_id: Optional[str] = None
    filled_qty: Optional[float] = 0.0
    remaining_qty: Optional[float] = 0.0
    average_price: Optional[float] = None

class Position(BaseModel):
    pos_key: str
    symbol: str
    side: str
    qty: Optional[float] = 0.0
    entry_price: Optional[float] = 0.0
    leverage: Optional[float] = 1.0
    unrealized_pnl: Optional[float] = 0.0
    liquidation_price: Optional[float] = None
    margin_type: str = 'isolated'
    sl: Optional[float] = None
    tp: Optional[float] = None
    sl_order_id: Optional[str] = None
    tp_order_id: Optional[str] = None
    status: str = 'OPENED'

class Trade(BaseModel):
    id: Optional[int] = None
    profile_id: int
    exchange_order_id: Optional[str] = None
    exchange: str
    symbol: str
    side: str
    qty: Optional[float] = 0.0
    entry_price: Optional[float] = 0.0
    exit_price: Optional[float] = None
    sl_price: Optional[float] = None
    tp_price: Optional[float] = None
    sl_order_id: Optional[str] = None
    tp_order_id: Optional[str] = None
    entry_time: Optional[int] = None
    exit_time: Optional[int] = None
    pnl: Optional[float] = 0.0
    exit_reason: Optional[str] = None
    status: str = 'OPENED'
    leverage: Optional[float] = 1.0
    timeframe: Optional[str] = '1h'
    pos_key: Optional[str] = None
    meta: Dict[str, Any] = Field(default_factory=dict)
