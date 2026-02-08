import ccxt.async_support as ccxt
import logging

class Trader:
    def __init__(self, exchange, dry_run=True):
        self.exchange = exchange
        self.dry_run = dry_run
        self.logger = logging.getLogger(__name__)

    async def set_mode(self, symbol, leverage):
        if self.dry_run: return
        try:
            # Set Leverage
            await self.exchange.set_leverage(leverage, symbol)
            # Set Isolated Margin (Exchange specific)
            try:
                await self.exchange.set_margin_mode('isolated', symbol)
            except Exception:
                pass # Might already be set or not supported by some endpoints
        except Exception as e:
            self.logger.warning(f"Failed to set mode for {symbol}: {e}")

    async def place_order(self, symbol, side, qty, order_type='market', price=None, sl=None, tp=None):
        """
        Places an order on the exchange.
        """

        if self.dry_run:
            self.logger.info(f"[DRY RUN] Would place {side} order for {symbol}: Qty={qty}, Type={order_type}, Price={price}, SL={sl}, TP={tp}")
            print(f"[DRY RUN] Placed {side} {symbol} {qty}")
            return {'id': 'dry_run_id', 'status': 'closed', 'filled': qty}

        params = {}
        if sl:
            params['stopLoss'] = str(sl)
            # Some exchanges require triggerPrice/stopPrice params specific to them
        if tp:
            params['takeProfit'] = str(tp)

        try:
            if order_type == 'market':
                order = await self.exchange.create_order(symbol, order_type, side, qty, params=params)
            elif order_type == 'limit':
                if price is None:
                    raise ValueError("Limit order requires price")
                order = await self.exchange.create_order(symbol, order_type, side, qty, price, params=params)
            
            self.logger.info(f"Order placed: {order['id']}")
            return order
        except Exception as e:
            self.logger.error(f"Failed to place order: {e}")
            return None

    async def cancel_all_orders(self, symbol):
        if self.dry_run:
            self.logger.info(f"[DRY RUN] Cancelling all orders for {symbol}")
            return
            
        try:
            await self.exchange.cancel_all_orders(symbol)
        except Exception as e:
            self.logger.error(f"Failed to cancel orders: {e}")

# Test Stub
if __name__ == "__main__":
    import asyncio
    
    class MockExchange:
        async def create_order(self, *args, **kwargs):
            return {'id': '123', 'status': 'open'}

    async def main():
        trader = Trader(MockExchange(), dry_run=True)
        await trader.place_order('BTC/USDT', 'buy', 0.001, sl=49000, tp=55000)

    asyncio.run(main())
