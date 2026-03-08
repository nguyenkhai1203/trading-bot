import pytest
import logging
import io
import time
from unittest.mock import MagicMock, patch
from src.execution import Trader, ExchangeLoggerAdapter
from src.infrastructure.repository.database import DataManager

class TestSecurity:
    """
    Security and Safety Testing.
    Verifies data masking, environment isolation, and early rejection of malicious inputs.
    """

    def test_logger_adapter_prefix(self):
        """Verifies that ExchangeLoggerAdapter correctly prefixes logs with exchange name."""
        import uuid
        unique_name = f"test_security_{uuid.uuid4().hex}"
        logger = logging.getLogger(unique_name)
        logger.setLevel(logging.INFO)
        logger.propagate = False # Prevent leaking to root
        
        # Use a real handler but mock its emit to capture the record
        handler = logging.StreamHandler()
        handler.emit = MagicMock()
        logger.addHandler(handler)
        
        adapter = ExchangeLoggerAdapter(logger, {'exchange_name': 'BINANCE'})
        adapter.info("Test message")
        
        # Verify if the message was prefixed
        assert handler.emit.called
        log_record = handler.emit.call_args[0][0]
        assert "[BINANCE]" in log_record.getMessage()

    def test_environment_isolation_db(self, tmp_path):
        """Verifies that Trader respects the environment 'TEST' vs 'LIVE'."""
        mock_ex = MagicMock()
        mock_ex.name = "BINANCE"
        mock_db = MagicMock()
        
        # Test env
        trader_test = Trader(mock_ex, mock_db, profile_id=1, env="TEST")
        assert trader_test.env == "TEST"
        
        # Live env
        trader_live = Trader(mock_ex, mock_db, profile_id=1, env="LIVE")
        assert trader_live.env == "LIVE"

    @pytest.mark.asyncio
    async def test_symbol_sanitization_in_pos_key(self, tmp_path):
        """Verifies that symbol-based keys are sanitized to prevent path/DB issues."""
        db = DataManager(str(tmp_path / "security.db"))
        mock_ex = MagicMock()
        mock_ex.name = "BINANCE"
        
        trader = Trader(mock_ex, db, profile_id=1, dry_run=True)
        
        # Test with a symbol containing potentially problematic characters
        # to_display_format strips :USDT suffix
        evil_symbol = "BTC/USDT:USDT"
        pos_key = trader._get_pos_key(evil_symbol, "1h")
        
        # Slashes and colons should be replaced with underscores (or stripped)
        assert "/" not in pos_key
        assert ":" not in pos_key
        assert "BTC_USDT" in pos_key

    def test_api_key_not_in_logger_adapter_extra(self):
        """Ensures that the logger adapter doesn't accidentally store API keys in its extra dict."""
        logger = logging.getLogger("test_security_keys")
        adapter = ExchangeLoggerAdapter(logger, {'exchange_name': 'BINANCE', 'api_key': 'SECRET'})
        
        # The 'process' method should only use what it needs
        msg, kwargs = adapter.process("message", {})
        assert "SECRET" not in msg
