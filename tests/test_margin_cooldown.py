"""
Tests for check_margin_error: 15-min cooldown + confidence-aware smart eviction.
"""
import unittest
from unittest.mock import MagicMock, AsyncMock, patch
import asyncio
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
from src.execution import Trader


def _make_trader():
    """Helper: create a minimal Trader with mocked exchange and DB."""
    mock_exchange = MagicMock()
    mock_exchange.name = 'BINANCE'
    mock_exchange.can_trade = True
    mock_exchange.is_public_only = False
    mock_exchange.milliseconds = MagicMock(return_value=1_000_000_000)
    mock_db = MagicMock()
    trader = Trader(mock_exchange, db=mock_db, profile_id=99, dry_run=False)
    trader.logger = MagicMock()
    trader.cancel_pending_order = AsyncMock()
    return trader


def _pending(order_id, conf, symbol='ETH/USDT'):
    return {
        'order_id': order_id,
        'symbol': symbol,
        'status': 'pending',
        'entry_confidence': conf,
    }


def _active_filled(conf, symbol='BTC/USDT'):
    return {
        'symbol': symbol,
        'status': 'filled',
        'entry_confidence': conf,
    }


INSUF_ERROR = "insufficient balance -2019"


class TestMarginCooldown(unittest.IsolatedAsyncioTestCase):

    async def test_cooldown_is_15_minutes(self):
        trader = _make_trader()
        before = time.time()
        await trader.check_margin_error(INSUF_ERROR)
        shared = Trader._shared_account_cache[trader.account_key]
        cooldown_until = shared.get('margin_cooldown_until', 0)
        # Should be ~900s from now (allow ±5s tolerance)
        self.assertAlmostEqual(cooldown_until - before, 900, delta=5)

    async def test_throttled_true_during_cooldown(self):
        trader = _make_trader()
        await trader.check_margin_error(INSUF_ERROR)
        self.assertTrue(trader.is_margin_throttled())

    async def test_throttled_false_when_expired(self):
        trader = _make_trader()
        # Manually set expired cooldown
        Trader._shared_account_cache[trader.account_key]['margin_cooldown_until'] = time.time() - 1
        self.assertFalse(trader.is_margin_throttled())

    async def test_non_margin_error_returns_false(self):
        trader = _make_trader()
        result = await trader.check_margin_error("Network timeout")
        self.assertFalse(result)

    # ---- Smart Eviction (new_confidence provided) ----

    async def test_evicts_only_orders_worse_than_new_signal(self):
        """New signal conf=0.7 → cancel pending with conf<0.7, keep conf>=0.7."""
        trader = _make_trader()
        trader.pending_orders = {
            'key_bad1': _pending('ord1', 0.5),
            'key_bad2': _pending('ord2', 0.6),
            'key_good': _pending('ord3', 0.8),
        }
        trader.active_positions = {}

        await trader.check_margin_error(INSUF_ERROR, new_confidence=0.7)

        # Should cancel 0.5 and 0.6 but NOT 0.8
        cancelled_keys = {call.args[0] for call in trader.cancel_pending_order.await_args_list}
        self.assertIn('key_bad1', cancelled_keys)
        self.assertIn('key_bad2', cancelled_keys)
        self.assertNotIn('key_good', cancelled_keys)
        self.assertEqual(trader.cancel_pending_order.await_count, 2)

    async def test_no_eviction_when_all_pending_are_better(self):
        """New signal conf=0.4, all pending have conf>=0.4 → nothing cancelled."""
        trader = _make_trader()
        trader.pending_orders = {
            'key_a': _pending('ord1', 0.7),
            'key_b': _pending('ord2', 0.8),
        }
        trader.active_positions = {}

        await trader.check_margin_error(INSUF_ERROR, new_confidence=0.4)

        trader.cancel_pending_order.assert_not_called()

    async def test_active_filled_positions_not_force_closed(self):
        """Filled positions that are worse than new signal get warned but NOT cancelled."""
        trader = _make_trader()
        trader.pending_orders = {}
        trader.active_positions = {
            'pos1': _active_filled(conf=0.3),
        }

        await trader.check_margin_error(INSUF_ERROR, new_confidence=0.8)

        # No cancel_pending_order called for filled positions
        trader.cancel_pending_order.assert_not_called()
        # Warning should have been logged
        trader.logger.warning.assert_called()

    async def test_high_conf_single_order_not_evicted(self):
        """conf=0.9 pending, new signal conf=0.85 → not cancelled."""
        trader = _make_trader()
        trader.pending_orders = {'key': _pending('ord1', 0.9)}
        trader.active_positions = {}

        await trader.check_margin_error(INSUF_ERROR, new_confidence=0.85)

        trader.cancel_pending_order.assert_not_called()

    # ---- Legacy mode (no new_confidence) ----

    async def test_legacy_mode_cancels_worst_below_06(self):
        trader = _make_trader()
        trader.pending_orders = {
            'key_low': _pending('ord1', 0.5),
            'key_mid': _pending('ord2', 0.7),
        }
        trader.active_positions = {}

        await trader.check_margin_error(INSUF_ERROR)  # no new_confidence

        # Only the worst one (0.5 < 0.6) should be cancelled
        self.assertEqual(trader.cancel_pending_order.await_count, 1)
        cancelled_key = trader.cancel_pending_order.await_args.args[0]
        self.assertEqual(cancelled_key, 'key_low')

    async def test_legacy_mode_no_cancel_if_all_above_06(self):
        trader = _make_trader()
        trader.pending_orders = {
            'key_a': _pending('ord1', 0.7),
            'key_b': _pending('ord2', 0.8),
        }
        trader.active_positions = {}

        await trader.check_margin_error(INSUF_ERROR)  # no new_confidence

        trader.cancel_pending_order.assert_not_called()


if __name__ == '__main__':
    unittest.main()
