import sys
import os
sys.path.append('src')
try:
    print("Attempting to import Trader from execution...")
    from execution import Trader
    print("Import successful")
except ImportError as e:
    print(f"ImportError caught: {e}")
    import traceback
    traceback.print_exc()
except Exception as e:
    print(f"Other exception caught: {e}")
    import traceback
    traceback.print_exc()
