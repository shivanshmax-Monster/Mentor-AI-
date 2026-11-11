import asyncio
import hashlib
import html
import json
import logging
import re
from typing import List, Dict, Any, Optional
import httpx

from config import config
from google_tasks_service import tasks_service
from gemini_service import gemini_service
from task_resolver import resolve_tasks_to_complete, chat_contexts, execute_completion

logger = logging.getLogger("TelegramService")

TELEGRAM_API_BASE = "https://api.telegram.org"


class TelegramService:
    def __init__(self):
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._polling_task: Optional[asyncio.Task] = None
        self._is_polling = False
        self._webhook_active = False
        self._last_update_id = 0
        self._bot_info: Optional[Dict[str, Any]] = None

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        """Stores the main FastAPI/Uvicorn asyncio event loop for thread-safe task scheduling."""
        self._loop = loop

    def get_token(self) -> str:
        token = config.TELEGRAM_BOT_TOKEN
        if not token or token.startswith("your_"):
            raise ValueError(
                "Telegram Bot Token missing! Please get a bot token from @BotFather on Telegram."
            )
        return token.strip()

    def get_bot_url(self, method: str, token: Optional[str] = None) -> str:
        t = token or self.get_token()
        return f"{TELEGRAM_API_BASE}/bot{t}/{method}"

    def verify_token(self, token: Optional[str] = None) -> Dict[str, Any]:
        """Synchronously verifies bot token and returns bot details."""
        url = self.get_bot_url("getMe", token=token)
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url)
            data = resp.json()
            if not data.get("ok"):
                error_desc = data.get("description", "Invalid Bot Token")
                raise ValueError(f"Telegram error: {error_desc}")
            self._bot_info = data.get("result")
            return self._bot_info

    def detect_chat_id_from_updates(self, token: Optional[str] = None) -> Optional[str]:
        """Checks recent Telegram updates synchronously to auto-detect a user's chat_id if missing."""
        if config.TELEGRAM_CHAT_ID:
            return config.TELEGRAM_CHAT_ID

        try:
            url = self.get_bot_url("getUpdates", token=token)
            with httpx.Client(timeout=8.0) as client:
                resp = client.get(url, params={"timeout": 0, "limit": 20})
                data = resp.json()
                if data.get("ok"):
                    for update in reversed(data.get("result", [])):
                        msg = update.get("message") or update.get("edited_message")
                        if msg and msg.get("chat", {}).get("id"):
                            chat_id = str(msg["chat"]["id"])
                            logger.info(f"Auto-detected Telegram Chat ID from updates: {chat_id}")
                            config.save_settings({"TELEGRAM_CHAT_ID": chat_id})
                            return chat_id
        except Exception as e:
            logger.warning(f"Could not detect Telegram chat ID from updates: {e}")
        return None

    def send_message(
        self,
        text: str,
        chat_id: Optional[str] = None,
        parse_mode: str = "HTML"
    ) -> Dict[str, Any]:
        """Synchronously sends a message to the specified Telegram chat_id."""
        target_chat_id = chat_id or config.TELEGRAM_CHAT_ID or self.detect_chat_id_from_updates()
        if not target_chat_id:
            raise ValueError(
                "Telegram Chat ID is not configured! Open your bot in Telegram and send /start first."
            )

        url = self.get_bot_url("sendMessage")
        payload = {
            "chat_id": target_chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True
        }

        with httpx.Client(timeout=10.0) as client:
            resp = client.post(url, json=payload)
            data = resp.json()
            if not data.get("ok"):
                if parse_mode == "HTML":
                    payload["parse_mode"] = ""
                    resp = client.post(url, json=payload)
                    data = resp.json()
                if not data.get("ok"):
                    raise ValueError(f"Telegram send failed: {data.get('description')}")
            return data.get("result", {})

    async def async_send_message(
        self,
        text: str,
        chat_id: Optional[str] = None,
        parse_mode: str = "HTML"
    ) -> Dict[str, Any]:
        """Asynchronously sends a message via httpx."""
        target_chat_id = chat_id or config.TELEGRAM_CHAT_ID
        if not target_chat_id:
            raise ValueError("Telegram Chat ID is not configured!")

        url = self.get_bot_url("sendMessage")
        payload = {
            "chat_id": target_chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
            data = resp.json()
            if not data.get("ok"):
                if parse_mode == "HTML":
                    payload["parse_mode"] = ""
                    resp = await client.post(url, json=payload)
                    data = resp.json()
                if not data.get("ok"):
                    raise ValueError(f"Telegram send failed: {data.get('description')}")
            return data.get("result", {})

    def format_reminder_text(self, pending_tasks: List[Dict[str, Any]]) -> str:
        """Formats pending tasks into an engaging Telegram HTML reminder."""
        if not pending_tasks:
            return "🎉 <b>Great job!</b> You have no pending tasks right now. Streak preserved! 🔥"

        lines = [
            "⏰ <b>HabitSync Reminder!</b>",
            f"You have <b>{len(pending_tasks)} pending task(s)</b> awaiting your attention:\n"
        ]

        for i, task in enumerate(pending_tasks[:5], 1):
            clean_title = html.escape(task.get("title", "Untitled"))
            lines.append(f"{i}. 📌 <b>{clean_title}</b>")

        if len(pending_tasks) > 5:
            lines.append(f"\n<i>...and {len(pending_tasks) - 5} more tasks in Google Tasks.</i>")

        lines.append(
            "\n👉 Reply <code>done</code> to complete your active task.\n"
            "👉 Or text <code>done all aws</code> / <code>done 2</code> to complete specific tasks!\n"
            "💬 Or ask me anything to help plan your session!"
        )
        return "\n".join(lines)

    def send_task_reminder(self, chat_id: Optional[str] = None, pending_tasks: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """Builds and sends a formatted task reminder."""
        if pending_tasks is None:
            pending_tasks = tasks_service.get_pending_tasks() if tasks_service.is_authenticated() else []
        text = self.format_reminder_text(pending_tasks)
        return self.send_message(text=text, chat_id=chat_id, parse_mode="HTML")

    # ========================================================================
    # Polling Engine (Receives Telegram Messages in Real-Time without ngrok)
    # ========================================================================
    def start_polling(self, loop: Optional[asyncio.AbstractEventLoop] = None):
        """Starts background long-polling on the main asyncio loop (thread-safe)."""
        if not config.TELEGRAM_BOT_TOKEN or config.TELEGRAM_BOT_TOKEN.startswith("your_"):
            logger.info("Telegram Bot Token not configured. Polling not started.")
            return

        if self._polling_task and not self._polling_task.done():
            return

        target_loop = loop
        if target_loop is None:
            try:
                target_loop = asyncio.get_running_loop()
            except RuntimeError:
                target_loop = self._loop

        if target_loop is None or target_loop.is_closed():
            logger.warning("Cannot start Telegram polling yet: no active event loop available.")
            return

        self._loop = target_loop
        self._is_polling = True

        def _spawn_task():
            if self._polling_task and not self._polling_task.done():
                return
            self._polling_task = target_loop.create_task(self._poll_loop())
            logger.info("Telegram polling started successfully.")

        try:
            running = asyncio.get_running_loop()
            if running is target_loop:
                _spawn_task()
                return
        except RuntimeError:
            pass

        target_loop.call_soon_threadsafe(_spawn_task)

    def stop_polling(self):
        """Stops background long-polling."""
        self._is_polling = False
        if self._polling_task and not self._polling_task.done():
            if self._loop and not self._loop.is_closed():
                self._loop.call_soon_threadsafe(self._polling_task.cancel)
            else:
                self._polling_task.cancel()
        logger.info("Telegram polling stopped.")

    def restart_polling(self):
        """Restarts polling (e.g., when bot token changes)."""
        self.stop_polling()
        self._last_update_id = 0
        self.start_polling()

    def is_polling(self) -> bool:
        return bool(self._webhook_active or (self._is_polling and self._polling_task and not self._polling_task.done()))

    def get_safe_webhook_secret(self) -> str:
        """Returns a Telegram-compliant webhook secret ([A-Za-z0-9_-] only)."""
        raw = config.TELEGRAM_WEBHOOK_SECRET.strip()
        if not raw:
            return ""
        if re.fullmatch(r"[A-Za-z0-9_-]{1,256}", raw):
            return raw
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:48]

    async def setup_webhook(self, base_url: Optional[str] = None) -> bool:
        """Registers HTTPS webhook with Telegram (recommended for free-tier cloud wakeups)."""
        target_base = (base_url or config.PUBLIC_BASE_URL).strip().rstrip("/")
        if not target_base.startswith("https://"):
            return False

        self.stop_polling()
        webhook_url = f"{target_base}/webhook/telegram"
        payload: Dict[str, Any] = {
            "url": webhook_url,
            "allowed_updates": ["message"],
        }
        safe_secret = self.get_safe_webhook_secret()
        if safe_secret:
            payload["secret_token"] = safe_secret

        try:
            async with httpx.AsyncClient(timeout=12.0) as client:
                resp = await client.post(self.get_bot_url("setWebhook"), json=payload)
                data = resp.json()
                if data.get("ok"):
                    self._webhook_active = True
                    logger.info(f"Telegram Webhook registered: {webhook_url}")
                    return True
                logger.warning(f"Telegram setWebhook failed: {data}")
        except Exception as e:
            logger.error(f"Error setting Telegram webhook: {e}")
        return False

    async def _init_webhook_or_poll(self):
        """Registers Telegram webhook, falling back automatically to polling on failure."""
        ok = await self.setup_webhook()
        if not ok:
            logger.warning("Webhook registration skipped or failed; falling back to Telegram polling.")
            self._webhook_active = False
            self.start_polling(loop=self._loop)

    def start_listener(self, loop: Optional[asyncio.AbstractEventLoop] = None):
        """Starts Telegram updates listener: HTTPS webhook on cloud if PUBLIC_BASE_URL is set, else long-polling."""
        if not config.TELEGRAM_BOT_TOKEN or config.TELEGRAM_BOT_TOKEN.startswith("your_"):
            logger.info("Telegram Bot Token not configured. Listener not started.")
            return

        if config.PUBLIC_BASE_URL.startswith("https://"):
            target_loop = loop or self._loop
            if target_loop and not target_loop.is_closed():
                self._loop = target_loop

                def _register():
                    target_loop.create_task(self._init_webhook_or_poll())

                try:
                    if asyncio.get_running_loop() is target_loop:
                        _register()
                        return
                except RuntimeError:
                    pass
                target_loop.call_soon_threadsafe(_register)
                return

        self._webhook_active = False
        self.start_polling(loop=loop)

    async def handle_webhook_update(self, update: Dict[str, Any]):
        """Handles an inbound update JSON pushed to /webhook/telegram."""
        update_id = update.get("update_id", 0)
        if update_id:
            self._last_update_id = max(self._last_update_id, update_id)
        message = update.get("message") or update.get("edited_message")
        if message:
            await self._handle_incoming_message(message)

    async def _poll_loop(self):
        """Continuous long-polling loop with exponential backoff on error."""
        logger.info("Starting Telegram long-polling loop...")

        # 1. Clear any leftover webhook so getUpdates works
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(self.get_bot_url("deleteWebhook"), json={"drop_pending_updates": False})
        except Exception as e:
            logger.warning(f"Failed to delete Telegram webhook: {e}")

        consecutive_errors = 0
        try:
            while self._is_polling:
                try:
                    token = config.TELEGRAM_BOT_TOKEN
                    if not token or token.startswith("your_"):
                        await asyncio.sleep(5)
                        continue

                    url = f"{TELEGRAM_API_BASE}/bot{token}/getUpdates"
                    params = {
                        "offset": self._last_update_id + 1,
                        "timeout": 15,
                        "allowed_updates": json.dumps(["message"])
                    }

                    async with httpx.AsyncClient(timeout=25.0) as client:
                        resp = await client.get(url, params=params)
                        if resp.status_code == 200:
                            data = resp.json()
                            consecutive_errors = 0
                            if data.get("ok"):
                                updates = data.get("result", [])
                                for update in updates:
                                    self._last_update_id = max(self._last_update_id, update.get("update_id", 0))
                                    message = update.get("message")
                                    if message:
                                        await self._handle_incoming_message(message)
                        else:
                            consecutive_errors += 1
                            logger.warning(f"Telegram polling HTTP {resp.status_code}: {resp.text}")
                            await asyncio.sleep(min(30, 2 ** consecutive_errors))

                except asyncio.CancelledError:
                    break
                except Exception as e:
                    consecutive_errors += 1
                    logger.error(f"Error in Telegram poll loop: {e}")
                    await asyncio.sleep(min(30, 2 ** consecutive_errors))
        finally:
            self._is_polling = False
            logger.info("Telegram polling loop exited.")

    async def _handle_incoming_message(self, message: Dict[str, Any]):
        """Processes an incoming message from Telegram without blocking the asyncio loop."""
        chat = message.get("chat", {})
        chat_id = str(chat.get("id"))
        text = message.get("text", "").strip()

        if not text or not chat_id:
            return

        # Auto-save chat ID if not set
        if not config.TELEGRAM_CHAT_ID:
            logger.info(f"Auto-detected Telegram Chat ID: {chat_id}. Saving to config.")
            await asyncio.to_thread(config.save_settings, {"TELEGRAM_CHAT_ID": chat_id})

        logger.info(f"Incoming Telegram message from {chat_id}: '{text}'")

        # 1. Handle commands
        if text.startswith("/start"):
            welcome_msg = (
                "👋 <b>Welcome to HabitSync AI!</b>\n\n"
                "I am your personal habit accountability partner synced directly with your <b>Google Tasks</b> and powered by <b>Google Gemini</b>.\n\n"
                "📌 <b>Commands:</b>\n"
                "• <code>/tasks</code> or <code>tasks</code> - View your active pending tasks\n"
                "• <code>done</code> - Complete your current top habit\n"
                "• <code>done all aws</code> - Complete all tasks for a topic\n"
                "• <code>done 2</code> - Complete task #2\n"
                "• Or just talk to me to plan, beat procrastination, and stay on track! 🔥"
            )
            await self.async_send_message(welcome_msg, chat_id=chat_id)
            return

        # 2. Get live pending tasks in worker thread (non-blocking)
        pending_tasks: List[Dict[str, Any]] = []
        tasks_error: Optional[str] = None
        is_auth = await asyncio.to_thread(tasks_service.is_authenticated)
        if is_auth:
            try:
                pending_tasks = await asyncio.to_thread(tasks_service.get_pending_tasks)
            except Exception as e:
                logger.error(f"Error fetching tasks: {e}")
                tasks_error = str(e)
        else:
            tasks_error = tasks_service.get_last_auth_error() or "Google Tasks is not authorized."

        if text.startswith("/tasks") or text.lower() in ["tasks", "list", "pending", "task list"]:
            if tasks_error:
                await self.async_send_message(
                    f"⚠️ <b>Google Tasks Disconnected</b>\n{html.escape(tasks_error)}",
                    chat_id=chat_id,
                )
            else:
                reminder_text = self.format_reminder_text(pending_tasks)
                await self.async_send_message(reminder_text, chat_id=chat_id)
            return

        # 3. Check for intelligent task completion
        last_assistant_msg = chat_contexts.get(f"telegram_{chat_id}", "")

        matched_tasks, reason = resolve_tasks_to_complete(text, pending_tasks, last_assistant_msg)
        if matched_tasks:
            reply = await asyncio.to_thread(execute_completion, matched_tasks, pending_tasks)
            chat_contexts[f"telegram_{chat_id}"] = reply
            await self.async_send_message(self._md_to_html(reply), chat_id=chat_id)
            return

        # 4. Conversational coaching via Gemini
        try:
            reply = await asyncio.to_thread(gemini_service.chat_with_tasks, text, pending_tasks)
            chat_contexts[f"telegram_{chat_id}"] = reply
            await self.async_send_message(self._md_to_html(reply), chat_id=chat_id)
        except Exception as e:
            logger.error(f"Gemini error: {e}")
            await self.async_send_message(
                "I couldn't reach Gemini right now. Reply <code>done</code> to complete your active task!",
                chat_id=chat_id
            )

    def _md_to_html(self, text: str) -> str:
        """Converts basic markdown bold/code from AI replies into safe Telegram HTML."""
        escaped = html.escape(text)
        escaped = re.sub(r'\*([^*\n]+)\*', r'<b>\1</b>', escaped)
        escaped = re.sub(r'`([^`\n]+)`', r'<code>\1</code>', escaped)
        return escaped


telegram_service = TelegramService()
