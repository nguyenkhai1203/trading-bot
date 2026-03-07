import pytest
from src.utils.symbol_helper import to_api_format, to_display_format, get_base_currency, get_quote_currency

def test_to_api_format():
    assert to_api_format("BTC/USDT") == "BTCUSDT"
    assert to_api_format("BTC/USDT:USDT") == "BTCUSDT"
    assert to_api_format("eth/usdt") == "ETHUSDT"
    assert to_api_format("") == ""
    assert to_api_format(None) == ""

def test_to_display_format():
    assert to_display_format("BTC/USDT") == "BTC/USDT"
    assert to_display_format("BTC/USDT:USDT") == "BTC/USDT"
    assert to_display_format("eth/usdt") == "ETH/USDT"
    assert to_display_format("") == ""

def test_get_base_currency():
    assert get_base_currency("BTC/USDT") == "BTC"
    assert get_base_currency("ETH/BTC") == "ETH"
    assert get_base_currency("SOL/USDT:USDT") == "SOL"

def test_get_quote_currency():
    assert get_quote_currency("BTC/USDT") == "USDT"
    assert get_quote_currency("BTC/USDT:USDT") == "USDT"
    assert get_quote_currency("ETH/BTC") == "BTC"
