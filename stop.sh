#!/bin/bash

# 🛑 Stop Trading Bot

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOT_PID_FILE="${SCRIPT_DIR}/bot.pid"
TELEGRAM_PID_FILE="${SCRIPT_DIR}/telegram.pid"

echo "🛑 Stopping Trading Bot..."

# Stop trading bot
if [ -f "$BOT_PID_FILE" ]; then
    BOT_PID=$(cat "$BOT_PID_FILE")
    if kill -0 $BOT_PID 2>/dev/null; then
        kill $BOT_PID
        echo "✅ Bot stopped (PID: $BOT_PID)"
    fi
    rm -f "$BOT_PID_FILE"
fi

# Stop Telegram bot
if [ -f "$TELEGRAM_PID_FILE" ]; then
    TELEGRAM_PID=$(cat "$TELEGRAM_PID_FILE")
    if kill -0 $TELEGRAM_PID 2>/dev/null; then
        kill $TELEGRAM_PID
        echo "✅ Telegram bot stopped (PID: $TELEGRAM_PID)"
    fi
    rm -f "$TELEGRAM_PID_FILE"
fi

# Kill any remaining Python processes from this repo
pkill -f "python.*bot.py" || true
pkill -f "python.*telegram_bot.py" || true

echo "✅ All bots stopped"
