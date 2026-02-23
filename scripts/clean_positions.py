import json
import os

def clean_nan(obj):
    if isinstance(obj, dict):
        return {k: clean_nan(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [clean_nan(i) for i in obj]
    elif isinstance(obj, float):
        if obj != obj:  # NaN check
            return None
        return obj
    else:
        return obj

def main():
    path = 'd:/code/tradingBot/src/positions.json'
    if not os.path.exists(path):
        print(f"File not found: {path}")
        return

    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        cleaned = clean_nan(data)
        
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(cleaned, f, indent=4)
        print("✅ Successfully cleaned NaN from positions.json")
    except Exception as e:
        print(f"❌ Error cleaning file: {e}")

if __name__ == '__main__':
    main()
