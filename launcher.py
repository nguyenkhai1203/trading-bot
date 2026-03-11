import subprocess
import sys
import time
import signal
import os
import platform

def main():
    print("Starting Advanced Trading Bot System...")
    
    # 1. Resolve Paths
    script_dir = os.path.dirname(os.path.abspath(__file__))
    src_dir = os.path.join(script_dir, "src")
    
    # 2. Add current directory to sys.path so 'src' is found as a package
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    
    # Also add src to path if needed for sub-scripts
    env = os.environ.copy()
    if "PYTHONPATH" in env:
        env["PYTHONPATH"] = f"{script_dir}{os.pathsep}{env['PYTHONPATH']}"
    else:
        env["PYTHONPATH"] = script_dir
        
    # 3. Detect Python command
    python_cmd = sys.executable
    
    # 4. Parse Arguments to pass along
    args = sys.argv[1:]
    
    # Enable/Disable Telegram via flag if needed, default is ON
    start_telegram = "--no-tg" not in args
    if not start_telegram:
        args.remove("--no-tg")

    # 5. Launch Trading Engine (Main Orchestrator)
    print(f"   - Starting Trading Engine ({os.path.join('src', 'main_orchestrator.py')})...")
    orchestrator_path = os.path.join(src_dir, "main_orchestrator.py")
    trading_proc = subprocess.Popen([python_cmd, orchestrator_path] + args, env=env)
    
    # Wait a moment for initialization
    time.sleep(3)
    
    # 6. Launch Telegram Bot
    telegram_proc = None
    if start_telegram:
        print(f"   - Starting Telegram Interface ({os.path.join('src', 'telegram_bot.py')})...")
        tg_path = os.path.join(src_dir, "telegram_bot.py")
        telegram_proc = subprocess.Popen([python_cmd, tg_path] + args, env=env)
    
    print("\n[OK] System fully operational.")
    print(f"OS: {platform.system()} {platform.release()}")
    print("Press CTRL+C to stop all components.\n")
    
    # 7. Stay active and monitor
    try:
        while True:
            # Check if processes are still alive
            if trading_proc.poll() is not None:
                print("!! Trading Engine stopped unexpectedly!")
                break
            if telegram_proc and telegram_proc.poll() is not None:
                print("!! Telegram Bot stopped unexpectedly!")
                break
            time.sleep(5)
    except KeyboardInterrupt:
        print("\n[SIGINT] Stopping system gracefully...")
        # Create stop signal file
        try:
            with open("stop_bot.txt", "w") as f:
                f.write("stop")
            print("   ↳ Stop signal sent to Orchestrator. Waiting for cleanup...")
            # Give it a few seconds to see the file and shut down
            time.sleep(5)
        except Exception as e:
            print(f"   ↳ [WARN] Failed to create stop file: {e}")
    finally:
        # 8. Clean termination
        print("   ↳ Terminating processes...")
        if telegram_proc:
            telegram_proc.terminate()
        trading_proc.terminate()
        
        # Wait for them to finish
        try:
            if telegram_proc:
                telegram_proc.wait(timeout=5)
            trading_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            print("   ↳ (Force killing persistent processes)")
            if telegram_proc:
                telegram_proc.kill()
            trading_proc.kill()
            
        # Delete stop signal file if it exists
        if os.path.exists("stop_bot.txt"):
            try: os.remove("stop_bot.txt")
            except: pass
            
    print("System shutdown complete.")

if __name__ == '__main__':
    main()