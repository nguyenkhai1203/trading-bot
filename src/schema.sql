CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    label TEXT,
    environment TEXT NOT NULL CHECK(environment IN ('LIVE', 'TEST')),
    exchange TEXT NOT NULL,
    api_key TEXT,
    api_secret TEXT,
    color TEXT DEFAULT 'white',
    is_active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL,
    exchange_order_id TEXT,           -- ID thực từ sàn
    exchange TEXT NOT NULL,           -- 'BINANCE' | 'BYBIT'
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    entry_price REAL,
    exit_price REAL,
    sl_price REAL, 
    tp_price REAL,
    sl_order_id TEXT, 
    tp_order_id TEXT, 
    pos_key TEXT,                     -- EXCHANGE_SYMBOL_TIMEFRAME (Slot ID)
    status TEXT DEFAULT 'OPENED'
        CHECK(status IN ('OPENED', 'ACTIVE', 'CLOSED', 'CANCELLED')),
    timeframe TEXT,
    entry_time INTEGER,
    exit_time INTEGER,
    pnl REAL, 
    exit_reason TEXT,
    meta_json TEXT,
    leverage REAL,
    FOREIGN KEY(profile_id) REFERENCES profiles(id)
);
CREATE INDEX IF NOT EXISTS idx_trades_active ON trades(status, exchange);
CREATE INDEX IF NOT EXISTS idx_trades_order_id ON trades(exchange_order_id, exchange);
CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_pos_key_active ON trades(profile_id, pos_key) WHERE status IN ('ACTIVE', 'OPENED');

CREATE TABLE IF NOT EXISTS ai_training_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER UNIQUE NOT NULL,
    snapshot_json TEXT,
    entry_confidence REAL,
    target_result REAL,
    FOREIGN KEY(trade_id) REFERENCES trades(id)
);

CREATE TABLE IF NOT EXISTS ohlcv_cache (
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    open REAL, 
    high REAL, 
    low REAL, 
    close REAL, 
    volume REAL,
    last_used_at INTEGER DEFAULT (strftime('%s', 'now')),
    PRIMARY KEY (symbol, timeframe, timestamp)
);
CREATE INDEX IF NOT EXISTS idx_ohlcv_last_used ON ohlcv_cache(last_used_at);

CREATE TABLE IF NOT EXISTS risk_metrics (
    profile_id INTEGER NOT NULL,
    environment TEXT NOT NULL CHECK(environment IN ('LIVE', 'TEST')),
    metric_name TEXT NOT NULL,
    value REAL,
    updated_at INTEGER,
    PRIMARY KEY (profile_id, environment, metric_name),
    FOREIGN KEY(profile_id) REFERENCES profiles(id)
);
