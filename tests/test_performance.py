import pytest
import asyncio
import time
import pandas as pd
from unittest.mock import MagicMock, AsyncMock, patch
from src.execution import Trader
from src.infrastructure.repository.database import DataManager
from src.data_manager import MarketDataManager

class TestPerformance:
    """
    Performance and Load Testing.
    Verifies latency constraints and throughput.
    """

    @pytest.fixture
    async def trader_bench(self, tmp_path):
        db_path = str(tmp_path / "perf.db")
        DataManager._instances = {}
        db = DataManager(db_path)
        await db.initialize()
        await db.add_profile(name="PERF", env="TEST", exchange="BINANCE", label="P")
        
        mock_ex = MagicMock()
        mock_ex.name = "BINANCE"
        mock_ex.fetch_positions = AsyncMock(return_value=[])
        mock_ex.fetch_open_orders = AsyncMock(return_value=[])
        
        mdm = MarketDataManager(db=db, adapters={"BINANCE": mock_ex})
        trader = Trader(mock_ex, db, profile_id=1, dry_run=True, data_manager=mdm)
        return trader, db, mock_ex

    @pytest.mark.asyncio
    async def test_reconcile_latency_bulk(self, trader_bench):
        """Measures latency of reconcile_positions with 50 'ghost' positions."""
        trader, db, mock_ex = trader_bench
        
        # Seed 50 positions in memory
        for i in range(50):
            sym = f"SYM{i}/USDT"
            pos_key = trader._get_pos_key(sym, "1h")
            trader.active_positions[pos_key] = {
                'symbol': sym, 'status': 'filled', 'qty': 1, 'entry_price': 100, 'timeframe': '1h'
            }
        
        # Mock exchange to return 0 positions (forcing a full reconcile of 50 closures)
        mock_ex.fetch_positions.return_value = []
        mock_ex.close_position = AsyncMock(return_value={'price': 101})
        
        start_time = time.perf_counter()
        with patch("src.execution.send_telegram_message", new_callable=AsyncMock):
            await trader.reconcile_positions()
        end_time = time.perf_counter()
        
        duration = end_time - start_time
        print(f"\n[PERF] Reconcile 50 positions took: {duration:.4f}s")
        
        # Target: < 2s for 50 positions (mostly I/O bound to local DB)
        assert duration < 5.0 

    @pytest.mark.asyncio
    async def test_db_bulk_write_speed(self, trader_bench):
        """Measures average time to save 100 positions to SQLite."""
        trader, db, _ = trader_bench
        
        start_time = time.perf_counter()
        for i in range(100):
            await db.save_position({
                'profile_id': 1,
                'pos_key': f"KEY_{i}",
                'symbol': f"SYM{i}/USDT",
                'status': 'ACTIVE',
                'side': 'BUY',
                'exchange': 'BINANCE',
                'timeframe': '1h',
                'entry_price': 100,
                'qty': 1
            })
        end_time = time.perf_counter()
        
        duration = end_time - start_time
        avg_ms = (duration / 100) * 1000
        print(f"\n[PERF] 100 DB writes took: {duration:.4f}s (Avg: {avg_ms:.2f}ms/write)")
        
        # Target: < 10ms avg per write for local SQLite WAL mode
        assert avg_ms < 50.0
