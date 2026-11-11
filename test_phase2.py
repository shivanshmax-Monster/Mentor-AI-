import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import text

import database
import main as app_main
import saas_routes
import security
from config import config
from scheduler_service import reminder_scheduler
from telegram_service import telegram_service


class Phase2MultiTenantTests(unittest.TestCase):
    """End-to-end verification suite for Phase 2 Multi-Tenant Web SaaS."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._db_path = Path(self._tmpdir.name) / "test_phase2.sqlite3"

        self._orig_env = {
            "ENCRYPTION_KEY": config.ENCRYPTION_KEY,
            "APP_SECRET_KEY": config.APP_SECRET_KEY,
            "DASHBOARD_PASSWORD": config.DASHBOARD_PASSWORD,
            "GOOGLE_WEB_CLIENT_ID": config.GOOGLE_WEB_CLIENT_ID,
            "GOOGLE_WEB_CLIENT_SECRET": config.GOOGLE_WEB_CLIENT_SECRET,
            "TELEGRAM_BOT_TOKEN": config.TELEGRAM_BOT_TOKEN,
            "DATABASE_URL": config.DATABASE_URL,
            "SAAS_MODE": config.SAAS_MODE,
        }

        config.ENCRYPTION_KEY = security.generate_fernet_key()
        config.APP_SECRET_KEY = "test-app-secret-signing-key"
        config.DASHBOARD_PASSWORD = "owner-secret-password"
        config.GOOGLE_WEB_CLIENT_ID = "test-web-client-id.apps.googleusercontent.com"
        config.GOOGLE_WEB_CLIENT_SECRET = "test-web-client-secret"
        config.TELEGRAM_BOT_TOKEN = "12345:test-bot-token"
        config.SAAS_MODE = False

        database.configure_database(f"sqlite:///{self._db_path.as_posix()}")
        database.init_db()
        self.client = TestClient(app_main.app)

    def tearDown(self):
        self.client.close()
        if database.engine is not None:
            database.engine.dispose()

        config.ENCRYPTION_KEY = self._orig_env["ENCRYPTION_KEY"]
        config.APP_SECRET_KEY = self._orig_env["APP_SECRET_KEY"]
        config.DASHBOARD_PASSWORD = self._orig_env["DASHBOARD_PASSWORD"]
        config.GOOGLE_WEB_CLIENT_ID = self._orig_env["GOOGLE_WEB_CLIENT_ID"]
        config.GOOGLE_WEB_CLIENT_SECRET = self._orig_env["GOOGLE_WEB_CLIENT_SECRET"]
        config.TELEGRAM_BOT_TOKEN = self._orig_env["TELEGRAM_BOT_TOKEN"]
        config.DATABASE_URL = self._orig_env["DATABASE_URL"]
        config.SAAS_MODE = self._orig_env["SAAS_MODE"]
        database.configure_database(config.DATABASE_URL)
        self._tmpdir.cleanup()

    def _signin_mock_google_user(self, email="alice@example.com", refresh_token="1//mock-refresh-token-xyz"):
        login_resp = self.client.get("/auth/google/login", follow_redirects=False)
        self.assertEqual(login_resp.status_code, 302)
        location = login_resp.headers["location"]
        state = location.split("state=")[1].split("&")[0]

        with (
            patch.object(
                saas_routes,
                "_exchange_google_code",
                return_value={"access_token": "mock-access", "refresh_token": refresh_token},
            ),
            patch.object(
                saas_routes,
                "_fetch_google_userinfo",
                return_value={"sub": "sub-alice-123", "email": email, "name": "Alice Test", "picture": ""},
            ),
        ):
            cb_resp = self.client.get(
                f"/auth/google/callback?code=auth-code-123&state={state}",
                follow_redirects=False,
            )
            self.assertEqual(cb_resp.status_code, 303)
            self.assertEqual(cb_resp.headers["location"], "/saas")

    def test_auth_isolation_between_owner_dashboard_and_saas_portal(self):
        # Owner admin endpoints stay locked by DASHBOARD_PASSWORD
        self.assertEqual(self.client.get("/api/status").status_code, 401)

        # SaaS landing & session check are open without owner password
        saas_page = self.client.get("/saas")
        self.assertEqual(saas_page.status_code, 200)
        self.assertIn("Connect with Google Tasks", saas_page.text)

        me = self.client.get("/api/saas/me")
        self.assertEqual(me.status_code, 200)
        self.assertFalse(me.json()["authenticated"])

        # SaaS protected endpoints require user's own Google session cookie
        self.assertEqual(self.client.get("/api/saas/tasks").status_code, 401)

    def test_oauth_encryption_and_part_b_schema_columns(self):
        raw_refresh = "1//super-secret-google-refresh-token"
        self._signin_mock_google_user(refresh_token=raw_refresh)

        me_resp = self.client.get("/api/saas/me")
        self.assertEqual(me_resp.status_code, 200)
        user_json = me_resp.json()["user"]
        self.assertEqual(user_json["email"], "alice@example.com")
        self.assertEqual(user_json["call_balance_seconds"], 0)
        self.assertFalse(user_json["is_calling_enabled"])
        self.assertIn("https://t.me/", user_json["telegram_deep_link"])

        # Inspect raw SQLite row to prove credentials are stored encrypted as ciphertext
        with database.engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT google_refresh_token_enc, is_calling_enabled, call_balance_seconds "
                    "FROM users WHERE email = :email"
                ),
                {"email": "alice@example.com"},
            ).fetchone()
            self.assertIsNotNone(row)
            cipher_refresh = row[0]
            self.assertNotEqual(cipher_refresh, raw_refresh)
            self.assertEqual(security.decrypt_secret(cipher_refresh), raw_refresh)
            self.assertEqual(bool(row[1]), False)
            self.assertEqual(int(row[2]), 0)

    def test_gemini_validation_and_e164_phone_validation_and_byok_encryption(self):
        self._signin_mock_google_user()

        # 1. Fake short Gemini key fails immediately
        bad_key_resp = self.client.post("/api/saas/validate-gemini", json={"api_key": "AIzaFakeKey123"})
        self.assertEqual(bad_key_resp.status_code, 400)

        # 2. Fake long Gemini key failing upstream call returns 400 and is NOT persisted
        with patch.object(
            saas_routes.gemini_service,
            "_generate_with_fallback",
            side_effect=RuntimeError("400 API_KEY_INVALID: API key not valid"),
        ):
            reject_resp = self.client.post(
                "/api/saas/onboarding",
                json={
                    "whatsapp_phone": "+919876543210",
                    "gemini_api_key": "AIzaSyFakeKeyNotAcceptedByGoogle123456",
                    "notification_channel": "both",
                },
            )
            self.assertEqual(reject_resp.status_code, 400)

        # 3. Phone number without '+' country code fails validation (422)
        bad_phone_resp = self.client.post(
            "/api/saas/onboarding",
            json={
                "whatsapp_phone": "9876543210",
                "notification_channel": "whatsapp",
            },
        )
        self.assertEqual(bad_phone_resp.status_code, 422)

        # 4. Valid phone + verified BYOK Gemini key saves cleanly and encrypts key in DB
        raw_gemini = "AIzaSyValidTenantGeminiKey99999999999999"
        with patch.object(saas_routes.gemini_service, "_generate_with_fallback", return_value="OK"):
            ok_resp = self.client.post(
                "/api/saas/onboarding",
                json={
                    "whatsapp_phone": "+91 98765-43210",
                    "gemini_api_key": raw_gemini,
                    "notification_channel": "both",
                    "reminder_interval_minutes": 120,
                },
            )
            self.assertEqual(ok_resp.status_code, 200)
            body = ok_resp.json()["user"]
            self.assertEqual(body["whatsapp_phone"], "+919876543210")
            self.assertTrue(body["gemini_key_verified"])
            self.assertEqual(body["masked_gemini_key"], "AIza...9999")
            self.assertNotIn(raw_gemini, ok_resp.text)

        with database.engine.connect() as conn:
            row = conn.execute(
                text("SELECT gemini_api_key_enc, whatsapp_number FROM users WHERE email = 'alice@example.com'")
            ).fetchone()
            self.assertNotEqual(row[0], raw_gemini)
            self.assertEqual(security.decrypt_secret(row[0]), raw_gemini)
            self.assertEqual(row[1], "whatsapp:+919876543210")

    def test_telegram_deeplink_and_isolated_tenant_routing_and_whatsapp_and_scheduler(self):
        self._signin_mock_google_user()
        with patch.object(saas_routes.gemini_service, "_generate_with_fallback", return_value="OK"):
            self.client.post(
                "/api/saas/onboarding",
                json={
                    "whatsapp_phone": "+919876543210",
                    "gemini_api_key": "AIzaSyTenantKeyForRoutingTests123456789",
                    "notification_channel": "both",
                    "reminder_interval_minutes": 60,
                    "reminder_start_hour": 0,
                    "reminder_end_hour": 23,
                },
            )

        me = self.client.get("/api/saas/me").json()["user"]
        link_token = me["telegram_link_token"]

        sent_telegram_messages = []

        async def fake_async_send(text, chat_id=None, parse_mode="HTML"):
            sent_telegram_messages.append((str(chat_id), text))
            return {"message_id": 1}

        # 1. Simulate Telegram `/start link_<token>` binding
        with patch.object(telegram_service, "async_send_message", side_effect=fake_async_send):
            asyncio.run(
                telegram_service._handle_incoming_message(
                    {"chat": {"id": 555666777}, "text": f"/start link_{link_token}"}
                )
            )

        me_after = self.client.get("/api/saas/me").json()["user"]
        self.assertTrue(me_after["telegram_linked"])
        self.assertEqual(me_after["telegram_chat_id"], "555666777")
        self.assertNotEqual(me_after["telegram_link_token"], link_token)  # Rotated after binding

        # 2. Simulate Telegram "done 1" and WhatsApp "tasks" strictly routed via UserGoogleTasksClient
        completed_calls = []

        class FakeUserTasksClient:
            def __init__(self, refresh_token):
                self.refresh_token = refresh_token

            def get_pending_tasks(self):
                return [{"id": "task-alice-1", "title": "Read 20 pages", "notes": "", "due": None, "status": "needsAction"}]

            def complete_task(self, task_id):
                completed_calls.append((self.refresh_token, task_id))
                return {"id": task_id, "title": "Read 20 pages", "status": "completed"}

        with (
            patch("tenant_router.UserGoogleTasksClient", FakeUserTasksClient),
            patch.object(telegram_service, "async_send_message", side_effect=fake_async_send),
        ):
            asyncio.run(
                telegram_service._handle_incoming_message(
                    {"chat": {"id": 555666777}, "text": "done 1"}
                )
            )
            self.assertEqual(completed_calls, [("1//mock-refresh-token-xyz", "task-alice-1")])
            self.assertIn("Read 20 pages", sent_telegram_messages[-1][1])

            wa_resp = self.client.post(
                "/webhook/whatsapp",
                data={"From": "whatsapp:+919876543210", "Body": "tasks"},
            )
            self.assertEqual(wa_resp.status_code, 200)
            self.assertIn("<Message>", wa_resp.text)
            self.assertIn("Read 20 pages", wa_resp.text)

        # 3. Verify multi-tenant scheduler picks up the bound SaaS user cleanly
        due = reminder_scheduler._collect_due_saas_users(datetime.now(timezone.utc))
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["email"], "alice@example.com")
        self.assertEqual(due[0]["telegram_chat_id"], "555666777")


if __name__ == "__main__":
    unittest.main(verbosity=2)

