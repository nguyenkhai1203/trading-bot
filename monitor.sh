#!/bin/bash

# 📊 Monitor Trading Bot Performance

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOT_PID_FILE="${SCRIPT_DIR}/bot.pid"
LOG_DIR="${SCRIPT_DIR}/logs"

if [ ! -f "$BOT_PID_FILE" ]; then
    echo "❌ Bot not running (no PID file)"
    exit 1
fi

BOT_PID=$(cat "$BOT_PID_FILE")

if ! kill -0 $BOT_PID 2>/dev/null; then
    echo "❌ Bot process not found (PID: $BOT_PID)"
    exit 1
fi

echo "📊 Trading Bot Monitor"
echo "═══════════════════════════════════════════════════════════"
echo ""

# Function to display stats
show_stats() {
    echo "🔍 Process Info (PID: $BOT_PID)"
    ps aux | grep $BOT_PID | grep -v grep | awk '{
        printf "  CPU: %.1f%%\n  Memory: %.1fMB\n  Runtime: %s\n", 
        $3, $6/1024, $9
    }'
    echo ""
    
    # Check file descriptors
    FD_COUNT=$(lsof -p $BOT_PID 2>/dev/null | wc -l)
    echo "  File Descriptors: $FD_COUNT"
    
    # Check network connections
    CONN_COUNT=$(lsof -p $BOT_PID -i 2>/dev/null | wc -l)
    echo "  Network Connections: $CONN_COUNT"
    echo ""
}

# Function to show logs
show_logs() {
    if [ -f "$LOG_DIR/bot.log" ]; then
        echo "📋 Recent Logs (last 20 lines):"
        echo "───────────────────────────────────────────────────────────"
        tail -20 "$LOG_DIR/bot.log" | sed 's/^/  /'
        echo ""
    fi
}

# Function to show errors
show_errors() {
    if [ -f "$LOG_DIR/bot.log" ]; then
        ERROR_COUNT=$(grep -c "ERROR\|Exception\|Traceback" "$LOG_DIR/bot.log" 2>/dev/null || echo 0)
        if [ $ERROR_COUNT -gt 0 ]; then
            echo "⚠️  Recent Errors ($ERROR_COUNT total):"
            echo "───────────────────────────────────────────────────────────"
            grep "ERROR\|Exception" "$LOG_DIR/bot.log" | tail -5 | sed 's/^/  /'
            echo ""
        fi
    fi
}

# Menu
while true; do
    clear
    show_stats
    show_logs
    show_errors
    
    echo "Options: [R]efresh [L]ogs [S]top [Q]uit"
    read -p "Choose: " choice
    
    case $choice in
        r|R) continue ;;
        l|L) tail -f "$LOG_DIR/bot.log" ;;
        s|S) 
            kill $BOT_PID
            echo "✅ Bot stopped"
            exit 0
            ;;
        q|Q) exit 0 ;;
    esac
done
