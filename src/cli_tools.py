import asyncio
import json
import os
from typing import Dict, Any

from data_manager import MarketDataManager
from execution import Trader
from config import TRADING_SYMBOLS, LEVERAGE, STOP_LOSS_PCT, TAKE_PROFIT_PCT


async def rebuild_positions_from_open_orders(save_path: str = None) -> Dict[str, Any]:
    mgr = MarketDataManager()
    ex = mgr.exchange
    trader = Trader(ex, dry_run=False)
    out_path = save_path or os.path.join(os.path.dirname(__file__), 'positions.json')
    positions = {}
    try:
        for s in TRADING_SYMBOLS:
            try:
                orders = await trader._execute_with_timestamp_retry(ex.fetch_open_orders, s)
            except Exception:
                orders = []
            for o in orders or []:
                side = o.get('side')
                amount = o.get('amount') or o.get('remaining') or o.get('filled')
                price = o.get('price')
                oid = str(o.get('id'))
                key = s
                positions[key] = {
                    'symbol': s,
                    'side': side.upper() if side else None,
                    'qty': float(amount) if amount else None,
                    'entry_price': float(price) if price else None,
                    'order_type': o.get('type'),
                    'status': 'pending',
                    'order_id': oid
                }
    finally:
        try:
            await ex.close()
        except Exception:
            pass

    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(positions, f, indent=4)
    return positions


async def reconstruct_position_from_trades(symbol: str) -> Dict[str, Any]:
    mgr = MarketDataManager()
    ex = mgr.exchange
    trader = Trader(ex, dry_run=False)
    try:
        trades = await trader._execute_with_timestamp_retry(ex.fetch_my_trades, symbol)
    except Exception:
        trades = []

    net_qty = 0.0
    net_cost = 0.0
    for t in sorted(trades or [], key=lambda x: x.get('timestamp') or 0):
        side = (t.get('side') or '').lower()
        qty = float(t.get('amount') or t.get('qty') or 0)
        price = float(t.get('price') or 0)
        if side == 'buy':
            net_qty += qty
            net_cost += price * qty
        elif side == 'sell':
            net_qty -= qty
            net_cost -= price * qty

    avg = None
    if abs(net_qty) > 1e-12:
        avg = abs(net_cost / net_qty)

    try:
        await ex.close()
    except Exception:
        pass

    if abs(net_qty) > 1e-12:
        return {
            'symbol': symbol,
            'side': 'BUY' if net_qty > 0 else 'SELL',
            'qty': abs(net_qty),
            'entry_price': avg,
            'status': 'filled',
            'order_type': 'market/agg_trades',
            'trades_agg_count': len(trades or [])
        }
    return {}


async def create_default_sl_tp_for_key(key: str):
    """Compute default SL/TP, set isolated+leverage, place SL then TP and persist ids."""
    mgr = MarketDataManager()
    ex = mgr.exchange
    trader = Trader(ex, dry_run=False)

    pos_path = os.path.join(os.path.dirname(__file__), 'positions.json')
    try:
        positions = json.load(open(pos_path))
    except Exception:
        positions = {}

    pos = positions.get(key) or trader.active_positions.get(key)
    if not pos:
        raise RuntimeError('position_not_found')

    side = pos.get('side')
    entry = float(pos.get('entry_price'))
    qty = float(pos.get('qty'))

    if side == 'SELL':
        sl_price = entry * (1.0 + STOP_LOSS_PCT)
        tp_price = entry * (1.0 - TAKE_PROFIT_PCT)
    else:
        sl_price = entry * (1.0 - STOP_LOSS_PCT)
        tp_price = entry * (1.0 + TAKE_PROFIT_PCT)

    pos['sl'] = round(sl_price, 6)
    pos['tp'] = round(tp_price, 6)

    trader.active_positions[key] = pos
    trader._save_positions()

    # ensure isolated + leverage
    lv = trader._clamp_leverage(LEVERAGE)
    await trader._ensure_isolated_and_leverage(key, lv)

    # create sl/tp (respects config AUTO_CREATE_SL_TP)
    res = await trader.recreate_missing_sl_tp(key, recreate_sl=True, recreate_tp=True, recreate_sl_force=True, recreate_tp_force=True)

    try:
        await ex.close()
    except Exception:
        pass

    return res


async def check_missing_sl_tp_all():
    mgr = MarketDataManager()
    ex = mgr.exchange
    trader = Trader(ex, dry_run=True)
    results = {}
    try:
        for key, pos in trader.active_positions.items():
            if pos.get('status') in ('filled', 'FILLED', True):
                res = await trader.check_missing_sl_tp(key)
                results[key] = res
    finally:
        try:
            await ex.close()
        except Exception:
            pass
    return results


async def get_market_info(symbol: str):
    """Fetch and print market limits and precision for a symbol."""
    from exchange_factory import get_exchange_adapter
    adapter = get_exchange_adapter()
    try:
        await adapter.exchange.load_markets()
        market = adapter.exchange.market(symbol)
        
        info = {
            "symbol": symbol,
            "min_qty": market['limits']['amount']['min'],
            "max_qty": market['limits']['amount']['max'],
            "qty_step": market['precision']['amount'],
            "min_notional": market['limits']['cost']['min'],
            "raw_info": market.get('info')
        }
        
        print(f"\n=== {symbol} Market Info ===")
        print(f"Min Qty: {info['min_qty']}")
        print(f"Max Qty: {info['max_qty']}")
        print(f"Qty Step: {info['qty_step']}")
        print(f"Min Notional: {info['min_notional']}")
        return info
    finally:
        await adapter.close()


if __name__ == '__main__':
    print('Utility module: import and call functions from an async runner.')
