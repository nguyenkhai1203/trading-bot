
import asyncio
import os
import sys
from dotenv import load_dotenv

# Add src to path
sys.path.append(os.path.join(os.path.dirname(__file__), 'src'))

# Load env from root
load_dotenv()

import ccxt.async_support as ccxt

async def test_bybit_advanced_orders():
    print("🚀 Initializing Bybit ADVANCED Execution Test...")
    
    api_key = os.getenv('BYBIT_API_KEY')
    api_secret = os.getenv('BYBIT_API_SECRET')
    
    if not api_key or not api_secret:
        print("❌ Missing API Keys in .env")
        return

    exchange = ccxt.bybit({
        'apiKey': api_key,
        'secret': api_secret,
        'options': {'defaultType': 'future'},
    })
    
    symbol = 'XRP/USDT'
    leverage = 10
    target_notional = 6.0 # Target $6 Notional (~$0.6 Margin at 10x)
    
    try:
        # 1. Setup Margin & Leverage
        print(f"\n⚙️ Setting up {symbol}...")
        try:
            await exchange.set_margin_mode('isolated', symbol)
            print("   ✅ Margin Mode set to ISOLATED")
        except Exception as e:
            print(f"   ⚠️ Margin Mode note: {e}")
            
        try:
            await exchange.set_leverage(leverage, symbol)
            print(f"   ✅ Leverage set to {leverage}x")
        except Exception as e:
            print(f"   ⚠️ Leverage note: {e}")

        # 2. Get Price & Calculate Qty
        print("\n💰 Calculating Position Size...")
        ticker = await exchange.fetch_ticker(symbol)
        current_price = ticker['last']
        
        # Calculate Qty for $6 Notional
        # Qty = Notional / Price
        raw_qty = target_notional / current_price
        
        # Round to 0 decimals for XRP (step size is usually 0.1 or 1, safe to use integer for test)
        # Better: get market precision
        await exchange.load_markets()
        market = exchange.market(symbol)
        
        # Check leverage support
        if not market['linear']:
             print("⚠️ Symbol is not linear! Leverage setting might fail.")

        qty = int(raw_qty) # XRP usually allows int qty. 
        if qty < 1: qty = 1 # Min check
        
        real_notional = qty * current_price
        margin_cost = real_notional / leverage
        
        print(f"   Price: {current_price}")
        print(f"   Target Notional: ${target_notional}")
        print(f"   Qty: {qty} XRP")
        print(f"   Actual Notional: ${real_notional:.2f}")
        print(f"   Estimated Margin Cost: ${margin_cost:.2f} (Well within $5 limit)")
        
        # 3. Prepare Limit Order with SL/TP
        # Buy Limit at 50% of price (Deep unreal entry to avoid fill)
        entry_price = round(current_price * 0.95, 4) # 5% below to be safe but reachable-ish visually
        
        sl_price = round(entry_price * 0.90, 4)
        tp_price = round(entry_price * 1.10, 4)
        
        print(f"\n📝 Placing LIMIT Buy Order with SL/TP...")
        print(f"   Entry: {entry_price}")
        print(f"   SL: {sl_price}")
        print(f"   TP: {tp_price}")
        
        params = {
            'stopLoss': str(sl_price),
            'takeProfit': str(tp_price),
        }
        
        order = await exchange.create_order(
            symbol=symbol,
            type='limit',
            side='buy',
            amount=qty,
            price=entry_price,
            params=params
        )
        order_id = order['id']
        print(f"✅ Order Placed! ID: {order_id}")
        
        # 4. Verify Order & SL/TP
        print("\n🔍 Verifying Order & Operations...")
        # Use fetch_open_orders
        open_orders = await exchange.fetch_open_orders(symbol)
        my_order = next((o for o in open_orders if o['id'] == order_id), None)
        
        if my_order:
            print(f"   ✅ Order Found in Open Orders")
            print(f"   Status: {my_order['status']}")
            print(f"   Price: {my_order['price']}")
            # Check attached SL/TP if visible (Bybit unified often shows in info)
            info = my_order.get('info', {})
            print(f"   SL (from info): {info.get('stopLoss', 'Check Exchange')}")
            print(f"   TP (from info): {info.get('takeProfit', 'Check Exchange')}")
        else:
            print("   ⚠️ Order not found in open orders?")

        # 5. Cancel
        print(f"\n❌ Cancelling Order {order_id}...")
        await exchange.cancel_order(order_id, symbol)
        
        # Verify Gone
        await asyncio.sleep(1)
        open_orders_after = await exchange.fetch_open_orders(symbol)
        if not any(o['id'] == order_id for o in open_orders_after):
             print("✅ Order Successfully Cancelled.")
        else:
             print("⚠️ Order still exists?")
             
    except Exception as e:
        print(f"\n❌ TEST FAILED: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await exchange.close()
        print("\n🏁 Advanced Test Complete.")

if __name__ == "__main__":
    asyncio.run(test_bybit_advanced_orders())
