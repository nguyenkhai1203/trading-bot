# -*- coding: utf-8 -*-
"""
TradeSyncHelper Utility
Standardizes bidirectional mapping between Database and Execution Engine.
Decouples Trader from DB schema details.
"""
from typing import Dict, Any, Optional
from datetime import datetime

class TradeSyncHelper:
    """Helper class to map between DB rows and internal Execution state."""

    @staticmethod
    def map_db_to_execution(row: dict, default_leverage: int = 1) -> dict:
        """
        Maps a database row (dict) to the format used in Trader.active_positions.
        
        Args:
            row: Dictionary containing DB fields (from DataManager.get_active_positions).
            default_leverage: Fallback leverage if missing in DB.
            
        Returns:
            dict: Standardized position for execution engine.
        """
        # Map DB status (ACTIVE/OPENED) to internal execution status
        status_raw = str(row.get('status', 'ACTIVE')).upper()
        mapped_status = 'pending' if status_raw == 'OPENED' else 'filled'
        
        # Parse meta metadata
        meta = row.get('meta') or {}
        
        return {
            "id": row.get('id'), # Keep DB ID for updates
            "profile_id": row.get('profile_id'),
            "symbol": row.get('symbol'),
            "side": row.get('side'),
            "qty": row.get('qty', 0),
            "entry_price": row.get('entry_price'),
            "sl": row.get('sl_price'),
            "tp": row.get('tp_price'),
            "timeframe": row.get('timeframe'),
            "status": mapped_status,
            "leverage": row.get('leverage', default_leverage),
            "order_id": row.get('exchange_order_id'),
            "sl_order_id": row.get('sl_order_id'),
            "tp_order_id": row.get('tp_order_id'),
            "timestamp": row.get('entry_time', 0),
            "signals_used": meta.get('signals_used', []),
            "entry_confidence": meta.get('entry_confidence', 0.5),
            "snapshot": meta.get('snapshot'),
            "pos_key": row.get('pos_key')
        }

    @staticmethod
    def map_execution_to_db(pos_key: str, pos: dict, profile_id: int, exchange_name: str) -> dict:
        """
        Maps Trader.active_positions entry to DataManager.save_position input format.
        
        Args:
            pos_key: Unique key for the position (e.g. BINANCE_BTCUSDT_1H).
            pos: The position dictionary from execution engine.
            profile_id: ID of the profile owning this position.
            exchange_name: Name of the exchange (e.g. BINANCE).
            
        Returns:
            dict: Formatted dictionary ready for db.save_position().
        """
        # Map internal status back to DB status
        internal_status = str(pos.get('status', 'filled')).lower()
        db_status = 'OPENED' if internal_status == 'pending' else 'ACTIVE'
        
        return {
            'id': pos.get('id'), # Use existing ID for UPDATE if present
            'profile_id': profile_id,
            'pos_key': pos_key,
            'exchange_order_id': pos.get('order_id'),
            'exchange': exchange_name,
            'symbol': pos.get('symbol'),
            'side': pos.get('side'),
            'entry_price': pos.get('entry_price'),
            'qty': pos.get('qty', 0),
            'leverage': pos.get('leverage', 1), # Should have been populated in map_db_to_execution
            'status': db_status,
            'sl_price': pos.get('sl'),
            'tp_price': pos.get('tp'),
            'timeframe': pos.get('timeframe'),
            'sl_order_id': pos.get('sl_order_id'),
            'tp_order_id': pos.get('tp_order_id'),
            'entry_time': pos.get('timestamp') or int(datetime.now().timestamp() * 1000),
            'meta': {
                'signals_used': pos.get('signals_used', []),
                'entry_confidence': pos.get('entry_confidence', 0.5),
                'snapshot': pos.get('snapshot')
            }
        }
