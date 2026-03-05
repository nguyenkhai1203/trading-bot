import subprocess
import sys
import os

# Helper function to check if running in a virtual environment
def is_venv():
    return (hasattr(sys, 'real_prefix') or
            (hasattr(sys, 'base_prefix') and sys.base_prefix != sys.prefix))

def run_tests():
    """Sets up the environment and runs all tests using pytest."""
    print("🚀 Starting Automated Test Suite...")
    
    # Check if running in a virtual environment
    if not is_venv():
        print("⚠️ Warning: Not running in a virtual environment. It's recommended to run tests within a venv.")
        # Optionally, you could exit here or prompt the user. For now, just a warning.
        # sys.exit(1)

    # 1. Set up PYTHONPATH to include 'src'
    env = os.environ.copy()
    
    # Improve path handling: get the directory of the current script
    script_dir = os.path.dirname(os.path.abspath(__file__))
    src_path = os.path.join(script_dir, "src")

    # Use os.pathsep for cross-platform compatibility
    if "PYTHONPATH" in env:
        env["PYTHONPATH"] = f"{src_path}{os.pathsep}{env['PYTHONPATH']}"
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
