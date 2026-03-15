
import os

def search_river(root_dir):
    print(f"Searching for 'RIVER' in {root_dir}")
    for root, dirs, files in os.walk(root_dir):
        if ".git" in dirs: dirs.remove(".git")
        if ".venv" in dirs: dirs.remove(".venv")
        if "__pycache__" in dirs: dirs.remove("__pycache__")
        
        for file in files:
            if file.endswith(('.py', '.txt', '.md', '.json')):
                path = os.path.join(root, file)
                try:
                    with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                        for i, line in enumerate(f, 1):
                            if "RIVER" in line:
                                print(f"MATCH: {path}:{i} -> {line.strip()}")
                except Exception as e:
                    pass

if __name__ == "__main__":
    search_river(".")
