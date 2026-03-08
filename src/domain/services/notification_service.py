import logging
from typing import Dict, Any, Optional
from src.infrastructure.notifications import notification

class NotificationService:
    """
    Domain service for sending notifications (Telegram, Terminal).
    Wraps the infrastructure-level notification functions.
    """
    def __init__(self):
        self.logger = logging.getLogger("NotificationService")

    async def notify_order_filled(self, trade: Any, score: float, dry_run: bool = False):
        """Send notification for a filled position."""
        try:
            terminal_msg, telegram_msg = notification.format_position_filled(
                symbol=trade.symbol,
                timeframe=trade.timeframe,
                side=trade.side,
                entry_price=trade.entry_price,
                size=trade.qty,
                notional=trade.entry_price * trade.qty,
                sl_price=trade.sl_price,
                tp_price=trade.tp_price,
                score=score,
                leverage=int(trade.leverage or 1),
                dry_run=dry_run,
                exchange_name=trade.exchange
            )
            self.logger.info(terminal_msg)
            await notification.send_telegram_message(telegram_msg)
        except Exception as e:
            self.logger.error(f"Failed to send filled notification: {e}")

    async def notify_order_pending(self, symbol: str, timeframe: str, side: str, price: float, sl: float, tp: float, score: float, leverage: int, dry_run: bool = False, exchange: str = ""):
        """Send notification for a pending limit order."""
        try:
            terminal_msg, telegram_msg = notification.format_pending_order(
                symbol=symbol,
                timeframe=timeframe,
                side=side,
                entry_price=price,
                sl_price=sl,
                tp_price=tp,
                score=score,
                leverage=leverage,
                dry_run=dry_run,
                exchange_name=exchange
            )
            self.logger.info(terminal_msg)
            await notification.send_telegram_message(telegram_msg)
        except Exception as e:
            self.logger.error(f"Failed to send pending notification: {e}")

    async def notify_order_cancelled(self, symbol: str, timeframe: str, side: str, price: float, reason: str, dry_run: bool = False, exchange: str = ""):
        """Send notification for a cancelled order."""
        try:
            terminal_msg, telegram_msg = notification.format_order_cancelled(
                symbol=symbol,
                timeframe=timeframe,
                side=side,
                entry_price=price,
                reason=reason,
                dry_run=dry_run,
                exchange_name=exchange
            )
            self.logger.info(terminal_msg)
            await notification.send_telegram_message(telegram_msg)
        except Exception as e:
            self.logger.error(f"Failed to send cancellation notification: {e}")

    async def notify_position_closed(
        self, 
        trade: Any, 
        exit_price: float, 
        pnl: float, 
        pnl_pct: float, 
        reason: str, 
        dry_run: bool = False
    ):
        """Send notification for a closed position."""
        try:
            from datetime import datetime
            
            # Convert timestamps if available
            entry_dt = datetime.fromtimestamp(trade.entry_time / 1000) if getattr(trade, 'entry_time', None) else None
            exit_dt = datetime.now() # Use now as fallback for sync-detected closure
            
            terminal_msg, telegram_msg = notification.format_position_closed(
                symbol=trade.symbol,
                timeframe=trade.timeframe,
                side=trade.side,
                entry_price=trade.entry_price,
                exit_price=exit_price,
                pnl=pnl,
                pnl_pct=pnl_pct,
                reason=reason,
                entry_time=entry_dt,
                exit_time=exit_dt,
                dry_run=dry_run,
                exchange_name=trade.exchange
            )
            self.logger.info(terminal_msg)
            await notification.send_telegram_message(telegram_msg)
        except Exception as e:
            self.logger.error(f"Failed to send closed notification: {e}")

    async def notify_generic(self, message: str):
        """Send a generic text notification."""
        self.logger.info(message)
        await notification.send_telegram_message(message)
