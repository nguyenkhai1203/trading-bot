#!/bin/bash

set -e

# 🚀 Trading Bot Launcher for Linux
# Usage: ./launcher.sh [--dry-run|--live] [--no-telegram]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
DRY_RUN="true"
SKIP_TELEGRAM="false"
LOG_DIR="${SCRIPT_DIR}/logs"
BOT_PID_FILE="${SCRIPT_DIR}/bot.pid"
TELEGRAM_PID_FILE="${SCRIPT_DIR}/telegram.pid"

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --live)
            DRY_RUN="false"
            shift
            ;;
        --dry-run)
            DRY_RUN="true"
            shift
            ;;
        --no-telegram)
            SKIP_TELEGRAM="true"
            shift
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

# Create log directory
mkdir -p "$LOG_DIR"

echo -e "${BLUE}╔════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║          🤖 Trading Bot Launcher - Linux${NC}"
echo -e "${BLUE}╚════════════════════════════════════════════════════════════╝${NC}"
echo ""

# Check Python
echo -e "${YELLOW}[1/6]${NC} Checking Python installation..."
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}❌ Python 3 not found${NC}"
    echo "   Install: sudo apt-get install python3-pip python3-venv"
    exit 1
fi
PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
echo -e "${GREEN}✅ Python ${PYTHON_VERSION}${NC}"

# Setup virtual environment
echo -e "${YELLOW}[2/6]${NC} Setting up virtual environment..."
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
    echo "   Created .venv"
fi
source .venv/bin/activate
echo -e "${GREEN}✅ Virtual environment active${NC}"

# Install dependencies
echo -e "${YELLOW}[3/6]${NC} Checking dependencies..."
pip install -q --upgrade pip setuptools wheel

# Check requirements
if [ -f "requirements.txt" ]; then
    pip install -q -r requirements.txt 2>/dev/null || {
        echo -e "${YELLOW}   ⚠️ Some dependencies failed to install (may continue)${NC}"
    }
fi

# Install optional performance libraries
python3 -c "import aiofiles" 2>/dev/null || {
    echo "   📦 Installing aiofiles (async I/O)..."
    pip install -q aiofiles
}

echo -e "${GREEN}✅ Dependencies installed${NC}"

# Load .env
echo -e "${YELLOW}[4/6]${NC} Loading configuration..."
if [ -f ".env" ]; then
    set -a
    source .env
    set +a
    echo -e "${GREEN}✅ Environment loaded from .env${NC}"
else
    echo -e "${YELLOW}⚠️  .env file not found (using defaults)${NC}"
fi

# Verify API credentials (if live mode)
echo -e "${YELLOW}[5/6]${NC} Verifying configuration..."
MODE_LABEL="🧪 DEMO"
if [ "$DRY_RUN" = "false" ]; then
    MODE_LABEL="✅ LIVE"
    
    if [ -z "$BINANCE_API_KEY" ] || [ "$BINANCE_API_KEY" = "your_api_key" ]; then
        echo -e "${RED}❌ LIVE mode requires valid BINANCE_API_KEY in .env${NC}"
        exit 1
    fi
    
    echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${RED}⚠️  WARNING: LIVE MODE ENABLED - REAL MONEY WILL BE TRADED!${NC}"
    echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    read -p "Type 'yes' to confirm and continue: " confirm
    if [ "$confirm" != "yes" ]; then
        echo -e "${YELLOW}Cancelled.${NC}"
        exit 0
    fi
fi

echo -e "${GREEN}✅ Configuration valid${NC}"

# Display status
echo -e "${YELLOW}[6/6]${NC} Starting Trading Bot..."
echo ""
echo -e "${BLUE}📊 Bot Configuration:${NC}"
echo -e "  Mode: $MODE_LABEL"
echo -e "  Python: $PYTHON_VERSION"
echo -e "  Symbols: ${TRADING_SYMBOLS:-BTC/USDT, ETH/USDT, XRP/USDT (default)}"
echo -e "  Timeframes: ${TRADING_TIMEFRAMES:-15m, 30m, 1h, 2h, 4h, 8h, 1d (default)}"
echo -e "  Telegram: ${SKIP_TELEGRAM:-enabled}"
echo -e "  Logs: $LOG_DIR/bot.log"
echo ""

# Clean up old PIDs if processes are dead
cleanup_pid() {
    local pid_file=$1
    if [ -f "$pid_file" ]; then
        local pid=$(cat "$pid_file")
        if ! kill -0 "$pid" 2>/dev/null; then
            rm -f "$pid_file"
        fi
    fi
}

cleanup_pid "$BOT_PID_FILE"
cleanup_pid "$TELEGRAM_PID_FILE"

# Start trading bot
echo -e "${GREEN}▶️  Starting Trading Bot...${NC}"
python3 src/bot.py 2>&1 | tee -a "$LOG_DIR/bot.log" &
echo $! > "$BOT_PID_FILE"
BOT_PID=$(cat "$BOT_PID_FILE")

# Wait a bit for bot to start
sleep 3

# Check if bot is running
if ! kill -0 $BOT_PID 2>/dev/null; then
    echo -e "${RED}❌ Bot failed to start (PID: $BOT_PID)${NC}"
    tail -20 "$LOG_DIR/bot.log"
    rm -f "$BOT_PID_FILE"
    exit 1
fi

echo -e "${GREEN}✅ Bot started (PID: $BOT_PID)${NC}"

# Start Telegram bot (optional)
if [ "$SKIP_TELEGRAM" = "false" ] && [ -n "$TELEGRAM_BOT_TOKEN" ]; then
    echo -e "${GREEN}▶️  Starting Telegram Bot...${NC}"
    sleep 2
    python3 src/telegram_bot.py 2>&1 | tee -a "$LOG_DIR/telegram.log" &
    echo $! > "$TELEGRAM_PID_FILE"
    TELEGRAM_PID=$(cat "$TELEGRAM_PID_FILE")
    
    if kill -0 $TELEGRAM_PID 2>/dev/null; then
        echo -e "${GREEN}✅ Telegram bot started (PID: $TELEGRAM_PID)${NC}"
    else
        echo -e "${YELLOW}⚠️  Telegram bot failed (continuing without it)${NC}"
        rm -f "$TELEGRAM_PID_FILE"
    fi
else
    echo -e "${YELLOW}⚠️  Telegram bot disabled${NC}"
fi

echo ""
echo -e "${BLUE}📋 Bot is running!${NC}"
echo -e "  View logs: ${YELLOW}tail -f $LOG_DIR/bot.log${NC}"
echo -e "  Stop bot:  ${YELLOW}kill $BOT_PID${NC}"
echo -e "  Stop all:  ${YELLOW}./stop.sh${NC}"
echo ""

# Keep running until bot dies
wait $BOT_PID
EXIT_CODE=$?

echo -e "${YELLOW}⚠️  Bot stopped (exit code: $EXIT_CODE)${NC}"

# Cleanup
rm -f "$BOT_PID_FILE"
if [ -f "$TELEGRAM_PID_FILE" ]; then
    kill $(cat "$TELEGRAM_PID_FILE") 2>/dev/null || true
    rm -f "$TELEGRAM_PID_FILE"
fi

exit $EXIT_CODE
