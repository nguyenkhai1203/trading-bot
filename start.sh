#!/bin/bash
echo "🚀 Launching Trading Bot..."
# Try python3, then python
if command -v python3 &>/dev/null; then
    python3 launcher.py "$@"
elif command -v python &>/dev/null; then
    python launcher.py "$@"
else
    echo "❌ Error: Python not found. Please install Python 3."
    exit 1
fi
