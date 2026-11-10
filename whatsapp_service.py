import json
import logging
from typing import List, Dict, Any, Optional
from twilio.rest import Client

from config import config

logger = logging.getLogger("WhatsAppService")


class WhatsAppService:
    def __init__(self):
        self._client = None
        self._last_sid = None
        self._last_token = None

    def get_client(self) -> Client:
        sid = config.TWILIO_ACCOUNT_SID
        token = config.TWILIO_AUTH_TOKEN
        if not sid or not token or sid.startswith("your_"):
            raise ValueError(
                "Twilio credentials missing! Please configure TWILIO_ACCOUNT_SID "
                "and TWILIO_AUTH_TOKEN in Settings."
            )

        if self._client is None or self._last_sid != sid or self._last_token != token:
            self._client = Client(sid, token)
            self._last_sid = sid
            self._last_token = token
        return self._client

    def send_message(
        self,
        to_number: str,
        body: str,
        content_sid: Optional[str] = None,
        content_variables: Optional[Dict[str, str]] = None
    ) -> Optional[str]:
        """
        Sends a WhatsApp message via Twilio.
        Handles both freeform body (active session) and Twilio Content Template (ContentSid).
        """
        if not to_number.startswith("whatsapp:"):
            to_number = f"whatsapp:{to_number}"

        from_number = config.TWILIO_WHATSAPP_NUMBER
        if not from_number.startswith("whatsapp:"):
            from_number = f"whatsapp:{from_number}"

        client = self.get_client()
        target_sid = content_sid or config.TWILIO_CONTENT_SID or "HXfe5ab5f00277942d4d4200328b4d403c"

        # 1. First attempt direct freeform body
        try:
            message = client.messages.create(
                from_=from_number,
                to=to_number,
                body=body
            )
            logger.info(f"WhatsApp message dispatched via body to {to_number}. SID: {message.sid}")
            return message.sid
        except Exception as e:
            err_str = str(e)
            # 2. If Twilio returns Error 21654 (ContentSid Required), use Sandbox Content Template
            if "ContentSid Required" in err_str or "21654" in err_str:
                logger.info(f"Twilio requires ContentSid. Dispatching via Content Template {target_sid}...")
                vars_payload = content_variables or {"1": "HabitSync Reminder", "2": body[:80]}
                try:
                    message = client.messages.create(
                        from_=from_number,
                        to=to_number,
                        content_sid=target_sid,
                        content_variables=json.dumps(vars_payload)
                    )
                    logger.info(f"WhatsApp template message dispatched to {to_number}. SID: {message.sid}")
                    return message.sid
                except Exception as template_err:
                    logger.error(f"Failed to send template WhatsApp message: {template_err}")
                    raise template_err
            raise e

    def test_connection(self, to_number: str) -> str:
        """Sends a verification test WhatsApp message."""
        body = "👋 HabitSync AI Test: Twilio WhatsApp integration is working! 🚀"
        target_sid = config.TWILIO_CONTENT_SID or "HXfe5ab5f00277942d4d4200328b4d403c"
        return self.send_message(
            to_number=to_number,
            body=body,
            content_sid=target_sid,
            content_variables={"1": "HabitSync AI", "2": "Connected"}
        )

    def format_reminder_text(self, pending_tasks: List[Dict[str, Any]]) -> str:
        """Formats pending tasks into an engaging WhatsApp reminder text."""
        if not pending_tasks:
            return "🎉 *Great job!* You have no pending tasks right now. Streak preserved! 🔥"

        lines = [
            "⏰ *HabitSync Reminder!*",
            f"You have *{len(pending_tasks)} pending task(s)* awaiting your attention:\n"
        ]

        for i, task in enumerate(pending_tasks[:5], 1):
            title = task.get("title", "Untitled")
            lines.append(f"{i}. 📌 *{title}*")

        if len(pending_tasks) > 5:
            lines.append(f"\n_...and {len(pending_tasks) - 5} more tasks in Google Tasks._")

        lines.append(
            "\n👉 Reply *done* to complete the top task.\n"
            "💬 Or ask me anything to help you plan or get started!"
        )
        return "\n".join(lines)

    def send_task_reminder(self, to_number: str, pending_tasks: List[Dict[str, Any]]) -> Optional[str]:
        """Builds and sends a formatted task reminder message."""
        body = self.format_reminder_text(pending_tasks)
        return self.send_message(to_number=to_number, body=body)


whatsapp_service = WhatsAppService()
