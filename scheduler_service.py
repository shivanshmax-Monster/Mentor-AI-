import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from config import config
from database import User, session_scope
from google_tasks_service import tasks_service
from telegram_service import telegram_service
from tenant_router import resolve_tasks_client_for_user
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


def _is_in_active_hours(tz_name: str, start_hour: int, end_hour: int) -> bool:
    """Checks whether the current local hour in `tz_name` falls within [start_hour, end_hour)."""
    tz = _get_timezone(tz_name)
    hour = datetime.now(tz).hour
    if start_hour <= end_hour:
        return start_hour <= hour < end_hour
    return hour >= start_hour or hour < end_hour


def _to_utc_aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Normalizes naive timestamps from SQLite back to timezone-aware UTC."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


class ReminderScheduler:
    """24/7 background scheduler supporting both Phase 1 owner & Phase 2 multi-tenant users."""

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
        return _is_in_active_hours(
            config.REMINDER_TIMEZONE,
            config.REMINDER_START_HOUR,
            config.REMINDER_END_HOUR,
        )

    async def _loop(self):
        await asyncio.sleep(15)
        while self._running:
            try:
                now_utc = datetime.now(timezone.utc)
                notified_targets: Set[Tuple[str, str]] = set()

                if config.REMINDER_ENABLED and self._in_active_hours():
                    elapsed_min = (
                        (now_utc - self._last_sent_utc).total_seconds() / 60.0
                        if self._last_sent_utc
                        else config.REMINDER_INTERVAL_MINUTES + 1
                    )
                    if elapsed_min >= config.REMINDER_INTERVAL_MINUTES:
                        notified_targets = await self._dispatch_if_pending(now_utc)

                await self._dispatch_saas_users(now_utc, already_notified=notified_targets)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Scheduler cycle error: {e}")

            await asyncio.sleep(60)

    async def _dispatch_if_pending(self, now_utc: datetime) -> Set[Tuple[str, str]]:
        notified: Set[Tuple[str, str]] = set()
        is_auth = await asyncio.to_thread(tasks_service.is_authenticated)
        if not is_auth:
            return notified

        pending = await asyncio.to_thread(tasks_service.get_pending_tasks)
        if not pending:
            logger.info("Scheduled check: 0 pending tasks. Skipping nudge.")
            self._last_sent_utc = now_utc
            return notified

        channel = config.NOTIFICATION_CHANNEL
        sent_any = False

        if channel in ("telegram", "both") and config.TELEGRAM_BOT_TOKEN:
            if not config.TELEGRAM_CHAT_ID:
                await asyncio.to_thread(telegram_service.detect_chat_id_from_updates)
            if config.TELEGRAM_CHAT_ID:
                try:
                    text = telegram_service.format_reminder_text(pending)
                    await telegram_service.async_send_message(text=text)
                    notified.add(("telegram", str(config.TELEGRAM_CHAT_ID)))
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
                notified.add(("whatsapp", config.USER_WHATSAPP_NUMBER))
                sent_any = True
            except Exception as e:
                logger.error(f"Scheduled WhatsApp nudge error: {e}")

        if sent_any:
            self._last_sent_utc = now_utc
            logger.info(f"Scheduled nudge sent ({len(pending)} pending tasks) via {channel}.")
        return notified

    def _collect_due_saas_users(self, now_utc: datetime) -> List[Dict[str, Any]]:
        """Reads eligible SaaS users due for a reminder nudge from the database."""
        due: List[Dict[str, Any]] = []
        with session_scope() as db:
            users = (
                db.query(User)
                .filter(User.reminder_enabled.is_(True), User.google_refresh_token_enc.isnot(None))
                .all()
            )
            for u in users:
                if not (u.telegram_chat_id or u.whatsapp_number):
                    continue
                if not _is_in_active_hours(
                    u.reminder_timezone or "Asia/Kolkata",
                    int(u.reminder_start_hour if u.reminder_start_hour is not None else 8),
                    int(u.reminder_end_hour if u.reminder_end_hour is not None else 22),
                ):
                    continue
                interval = int(u.reminder_interval_minutes or 240)
                last_sent = _to_utc_aware(u.last_reminder_sent_at)
                elapsed = (now_utc - last_sent).total_seconds() / 60.0 if last_sent else interval + 1
                if elapsed >= interval:
                    due.append(
                        {
                            "id": u.id,
                            "email": u.email,
                            "channel": u.notification_channel,
                            "telegram_chat_id": u.telegram_chat_id,
                            "whatsapp_number": u.whatsapp_number,
                            "refresh_token_enc": u.google_refresh_token_enc,
                        }
                    )
        return due

    def _mark_saas_user_reminded(self, user_id: str, now_utc: datetime) -> None:
        with session_scope() as db:
            user = db.query(User).filter(User.id == user_id).first()
            if user:
                user.last_reminder_sent_at = now_utc

    async def _dispatch_saas_users(
        self,
        now_utc: datetime,
        already_notified: Optional[Set[Tuple[str, str]]] = None,
    ) -> None:
        notified = already_notified or set()
        due_users = await asyncio.to_thread(self._collect_due_saas_users, now_utc)
        for u in due_users:
            try:
                stub = User(id=u["id"], email=u["email"], google_refresh_token_enc=u["refresh_token_enc"])
                client = await asyncio.to_thread(resolve_tasks_client_for_user, stub)
                pending = await asyncio.to_thread(client.get_pending_tasks)
                if not pending:
                    await asyncio.to_thread(self._mark_saas_user_reminded, u["id"], now_utc)
                    continue

                sent = False
                channel = u["channel"]
                tg_id = u["telegram_chat_id"]
                if (
                    channel in ("telegram", "both")
                    and tg_id
                    and config.TELEGRAM_BOT_TOKEN
                    and ("telegram", str(tg_id)) not in notified
                ):
                    try:
                        text = telegram_service.format_reminder_text(pending)
                        await telegram_service.async_send_message(text=text, chat_id=str(tg_id))
                        sent = True
                    except Exception as exc:
                        logger.error(f"SaaS Telegram nudge error for {u['email']}: {exc}")

                wa_num = u["whatsapp_number"]
                if (
                    channel in ("whatsapp", "both")
                    and wa_num
                    and ("whatsapp", wa_num) not in notified
                ):
                    try:
                        await asyncio.to_thread(whatsapp_service.send_task_reminder, wa_num, pending)
                        sent = True
                    except Exception as exc:
                        logger.error(f"SaaS WhatsApp nudge error for {u['email']}: {exc}")

                if sent:
                    await asyncio.to_thread(self._mark_saas_user_reminded, u["id"], now_utc)
                    logger.info(f"SaaS nudge dispatched for {u['email']} ({len(pending)} pending tasks)")
            except Exception as exc:
                logger.error(f"SaaS scheduler error for {u.get('email')}: {exc}")


reminder_scheduler = ReminderScheduler()
