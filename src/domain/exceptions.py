class TradingError(Exception):
    """Base class for trading exceptions."""
    pass

class InsufficientFundsError(TradingError):
    """Raised when the account has insufficient balance to open a position."""
    pass

class ReversalError(TradingError):
    """Raised when a reversal fails."""
    pass
