import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from config import config
from google_tasks_service import tasks_service
from telegram_service import telegram_service
from whatsapp_service import whatsapp_service

logger = logging.getLogger("SchedulerService")


def _get_timezone(tz_name: str):
    """Returns ZoneInfo if available, with safe fallback for Asia/Kolkata or UTC."""
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        if tz_name == "Asia/Kolkata":
            return timezone(timedelta(hours=5, minutes=30))
        return timezone.utc


class ReminderScheduler:
    """Lightweight 24/7 background scheduler for proactive task accountability nudges."""

    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._last_sent_utc: Optional[datetime] = None

    def start(self, loop: Optional[asyncio.AbstractEventLoop] = None):
        if self._task and not self._task.done():
            return
        target_loop = loop or asyncio.get_running_loop()
        self._running = True
        self._task = target_loop.create_task(self._loop())
        logger.info("Background reminder scheduler initialized.")

    def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("Background reminder scheduler stopped.")

    def get_status(self) -> Dict[str, Any]:
        return {
            "enabled": config.REMINDER_ENABLED,
            "interval_minutes": config.REMINDER_INTERVAL_MINUTES,
            "timezone": config.REMINDER_TIMEZONE,
            "active_hours": f"{config.REMINDER_START_HOUR:02d}:00-{config.REMINDER_END_HOUR:02d}:00",
            "last_sent_utc": self._last_sent_utc.isoformat() if self._last_sent_utc else None,
            "worker_alive": bool(self._running and self._task and not self._task.done()),
        }

    def _in_active_hours(self) -> bool:
        tz = _get_timezone(config.REMINDER_TIMEZONE)
        now_local = datetime.now(tz)
        hour = now_local.hour
        start = config.REMINDER_START_HOUR
        end = config.REMINDER_END_HOUR
        if start <= end:
            return start <= hour < end
        return hour >= start or hour < end

    async def _loop(self):
        # Short initial delay on boot so startup settles cleanly
        await asyncio.sleep(15)
        while self._running:
            try:
                if config.REMINDER_ENABLED and self._in_active_hours():
                    now_utc = datetime.now(timezone.utc)
                    elapsed_min = (
                        (now_utc - self._last_sent_utc).total_seconds() / 60.0
                        if self._last_sent_utc
                        else config.REMINDER_INTERVAL_MINUTES + 1
                    )
                    if elapsed_min >= config.REMINDER_INTERVAL_MINUTES:
                        await self._dispatch_if_pending(now_utc)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Scheduler cycle error: {e}")

            await asyncio.sleep(60)

    async def _dispatch_if_pending(self, now_utc: datetime):
        is_auth = await asyncio.to_thread(tasks_service.is_authenticated)
        if not is_auth:
            return

        pending = await asyncio.to_thread(tasks_service.get_pending_tasks)
        if not pending:
            logger.info("Scheduled check: 0 pending tasks. Skipping nudge.")
            self._last_sent_utc = now_utc
            return

        channel = config.NOTIFICATION_CHANNEL
        sent_any = False

        if channel in ("telegram", "both") and config.TELEGRAM_BOT_TOKEN:
            if not config.TELEGRAM_CHAT_ID:
                await asyncio.to_thread(telegram_service.detect_chat_id_from_updates)
            if config.TELEGRAM_CHAT_ID:
                try:
                    text = telegram_service.format_reminder_text(pending)
                    await telegram_service.async_send_message(text=text)
                    sent_any = True
                except Exception as e:
                    logger.error(f"Scheduled Telegram nudge error: {e}")

        if channel in ("whatsapp", "both") and config.USER_WHATSAPP_NUMBER:
            try:
                await asyncio.to_thread(
                    whatsapp_service.send_task_reminder,
                    config.USER_WHATSAPP_NUMBER,
                    pending,
                )
                sent_any = True
            except Exception as e:
                logger.error(f"Scheduled WhatsApp nudge error: {e}")

        if sent_any:
            self._last_sent_utc = now_utc
            logger.info(f"Scheduled nudge sent ({len(pending)} pending tasks) via {channel}.")


reminder_scheduler = ReminderScheduler()

