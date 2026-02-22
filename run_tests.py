import subprocess
import sys
import os

def run_tests():
    """Sets up the environment and runs all tests using pytest."""
    print("🚀 Starting Automated Test Suite...")
    
    # 1. Set up PYTHONPATH to include 'src'
    env = os.environ.copy()
    src_path = os.path.join(os.getcwd(), "src")
    if "PYTHONPATH" in env:
        env["PYTHONPATH"] = f"{src_path};{env['PYTHONPATH']}"
    else:
        env["PYTHONPATH"] = src_path

    # 2. Command to run pytest
    # We use -v for verbose output and -s to see print statements if needed
    cmd = [sys.executable, "-m", "pytest", "tests/", "-v"]
    
    try:
        # Run pytest
        result = subprocess.run(cmd, env=env)
        
        if result.returncode == 0:
            print("\n✅ ALL TESTS PASSED SUCCESSFULLY!")
        else:
            print(f"\n❌ TESTS FAILED with exit code {result.returncode}")
            
        return result.returncode
    except Exception as e:
        print(f"❌ Error during test execution: {e}")
        return 1

if __name__ == "__main__":
    exit_code = run_tests()
    sys.exit(exit_code)
