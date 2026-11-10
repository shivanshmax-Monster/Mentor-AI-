import sys
import io

# Ensure UTF-8 output on Windows consoles
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import argparse
import logging
from pprint import pprint

from config import config
from google_tasks_service import tasks_service
from gemini_service import gemini_service
from whatsapp_service import whatsapp_service
from telegram_service import telegram_service
from task_resolver import resolve_tasks_to_complete, execute_completion

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("HabitSyncCLI")


def check_status():
    print("\n" + "=" * 60)
    print("      HabitSync AI - Phase 1 Environment Status Check      ")
    print("=" * 60)
    val = config.validate()
    for k, v in val.items():
        icon = "[READY]" if v else "[MISSING]"
        print(f"  {k:<28} : {icon}")

    print("\nDetailed paths & settings:")
    print(f"  Credentials file path        : {config.get_credentials_path()}")
    print(f"  Token file path              : {config.get_token_path()}")
    print(f"  Notification Channel         : {config.NOTIFICATION_CHANNEL.upper()}")
    print(f"  Telegram Bot Token           : {'[Configured]' if config.TELEGRAM_BOT_TOKEN else '[Not set]'}")
    print(f"  Telegram Chat ID             : {config.TELEGRAM_CHAT_ID or '[Auto-detect upon /start]'}")
    print(f"  Twilio WhatsApp Number       : {config.TWILIO_WHATSAPP_NUMBER}")
    print(f"  Target User Number           : {config.USER_WHATSAPP_NUMBER or '(Not set)'}")
    print(f"  Gemini Model                 : {config.GEMINI_MODEL}")
    print("=" * 60 + "\n")


def test_auth():
    print("\n[1] Testing Google Tasks OAuth Flow...")
    try:
        creds = tasks_service.get_credentials()
        print("[SUCCESS] Successfully authenticated Google Tasks!")
        print(f"   Token valid: {creds.valid}")
        print(f"   Token expiry: {creds.expiry}")
    except Exception as e:
        print(f"[ERROR] Auth failed: {e}")


def test_list_tasks():
    print("\n[2] Fetching Pending Google Tasks...")
    try:
        tasks = tasks_service.get_pending_tasks()
        print(f"[SUCCESS] Found {len(tasks)} pending task(s):")
        for i, t in enumerate(tasks, 1):
            print(f"   {i}. [{t['id']}] {t['title']} (Due: {t['due'] or 'None'})")
    except Exception as e:
        print(f"[ERROR] Failed to fetch tasks: {e}")


def test_add_task(title: str, notes: str = None):
    print(f"\n[3] Adding task: '{title}'...")
    try:
        created = tasks_service.create_task(title=title, notes=notes)
        print(f"[SUCCESS] Task created successfully!")
        print(f"   ID: {created.get('id')}")
        print(f"   Title: {created.get('title')}")
    except Exception as e:
        print(f"[ERROR] Failed to add task: {e}")


def test_complete_task(task_id: str = None):
    print(f"\n[4] Completing Task...")
    try:
        if not task_id:
            pending = tasks_service.get_pending_tasks()
            if not pending:
                print("[WARNING] No pending tasks found to complete!")
                return
            task_id = pending[0]["id"]
            print(f"   (No ID provided, selecting top pending task: '{pending[0]['title']}')")

        completed = tasks_service.complete_task(task_id)
        print(f"[SUCCESS] Task '{completed.get('title')}' ({task_id}) marked as COMPLETED!")
    except Exception as e:
        print(f"[ERROR] Failed to complete task: {e}")


def test_gemini(prompt: str = "Hey, what tasks do I have left today?"):
    print(f"\n[5] Testing Gemini Conversation with Tasks Context...")
    print(f"   User prompt: \"{prompt}\"")
    try:
        try:
            pending = tasks_service.get_pending_tasks()
        except Exception:
            pending = [
                {"id": "dummy_1", "title": "AWS Daily Streak Check-in", "due": None},
                {"id": "dummy_2", "title": "Review System Design Chapter 3", "due": None}
            ]
            print("   (Using mock tasks context since Google Tasks is not yet connected)")

        reply = gemini_service.chat_with_tasks(prompt, pending)
        print("\nGemini Response:")
        print("-" * 50)
        print(reply)
        print("-" * 50)
    except Exception as e:
        print(f"[ERROR] Gemini failed: {e}")


def test_send_reminder():
    print(f"\n[6] Sending WhatsApp Reminder via Twilio...")
    if not config.USER_WHATSAPP_NUMBER:
        print("[ERROR] USER_WHATSAPP_NUMBER is not configured in .env!")
        return

    try:
        pending = tasks_service.get_pending_tasks()
    except Exception:
        pending = [{"id": "sample", "title": "Sample Streak Task", "due": None}]
        print("   (Using sample task for reminder)")

    channel = config.NOTIFICATION_CHANNEL
    print(f"   Active Channel: {channel.upper()}")

    if channel in ["whatsapp", "both"]:
        try:
            sid = whatsapp_service.send_task_reminder(config.USER_WHATSAPP_NUMBER, pending)
            print(f"[SUCCESS] WhatsApp reminder dispatched! SID: {sid}")
        except Exception as e:
            print(f"[ERROR] WhatsApp dispatch failed: {e}")

    if channel in ["telegram", "both"]:
        try:
            res = telegram_service.send_task_reminder(pending_tasks=pending)
            print(f"[SUCCESS] Telegram reminder dispatched! Message ID: {res.get('message_id')}")
        except Exception as e:
            print(f"[ERROR] Telegram dispatch failed: {e}")


def test_telegram():
    print("\n[6.5] Testing Telegram Bot Connection...")
    try:
        bot = telegram_service.verify_token()
        print(f"[SUCCESS] Connected to Telegram Bot: @{bot.get('username')} ({bot.get('first_name')})")
        if config.TELEGRAM_CHAT_ID:
            print(f"   Sending test ping to Chat ID: {config.TELEGRAM_CHAT_ID}...")
            telegram_service.send_message("👋 HabitSync AI Test from CLI!")
            print("[SUCCESS] Test ping delivered to your Telegram!")
        else:
            print("   (Chat ID not set yet. Open Telegram, search your bot, and tap /start to auto-link)")
    except Exception as e:
        print(f"[ERROR] Telegram verification failed: {e}")


def simulate_incoming_message(msg: str):
    print(f"\n[7] Simulating Incoming Message: '{msg}'...")
    from main import LIST_KEYWORDS

    normalized = msg.strip().lower()

    try:
        pending = tasks_service.get_pending_tasks()
    except Exception:
        pending = [
            {"id": "demo_1", "title": "AWS 90-Day Streak Check-in"},
            {"id": "demo_2", "title": "DSA LeetCode Problem"}
        ]
        print("   (Using simulated pending tasks)")

    if any(k in normalized for k in LIST_KEYWORDS):
        print("Action: LIST REQUEST DETECTED!")
        print(whatsapp_service.format_reminder_text(pending))
        return

    matched, reason = resolve_tasks_to_complete(msg, pending)
    if matched:
        print(f"Action: COMPLETION DETECTED! Reason: {reason}")
        for t in matched:
            print(f" - Matched Task: '{t['title']}' ({t['id']})")
    else:
        print("Action: CONVERSATIONAL QUERY -> Routing to Gemini:")
        try:
            reply = gemini_service.chat_with_tasks(msg, pending)
            print(f"Bot reply: {reply}")
        except Exception as e:
            print(f"Gemini error: {e}")


def export_cloud_env():
    """Exports cloud-ready single-line env vars (including GOOGLE_TOKEN_JSON) to .cloud-env.txt."""
    import json
    from pathlib import Path

    out_path = Path(__file__).resolve().parent / ".cloud-env.txt"
    lines = [
        "# Paste these environment variables into Render / Railway / Cloud Run",
        "CLOUD_ENV=true",
        f"GEMINI_API_KEY={config.GEMINI_API_KEY}",
        f"GEMINI_MODEL={config.GEMINI_MODEL}",
        f"NOTIFICATION_CHANNEL={config.NOTIFICATION_CHANNEL}",
        f"TELEGRAM_BOT_TOKEN={config.TELEGRAM_BOT_TOKEN}",
        f"TELEGRAM_CHAT_ID={config.TELEGRAM_CHAT_ID}",
        f"TWILIO_ACCOUNT_SID={config.TWILIO_ACCOUNT_SID}",
        f"TWILIO_AUTH_TOKEN={config.TWILIO_AUTH_TOKEN}",
        f"TWILIO_WHATSAPP_NUMBER={config.TWILIO_WHATSAPP_NUMBER}",
        f"TWILIO_CONTENT_SID={config.TWILIO_CONTENT_SID}",
        f"USER_WHATSAPP_NUMBER={config.USER_WHATSAPP_NUMBER}",
        "REMINDER_ENABLED=true",
        f"REMINDER_INTERVAL_MINUTES={config.REMINDER_INTERVAL_MINUTES}",
        f"REMINDER_TIMEZONE={config.REMINDER_TIMEZONE}",
        f"REMINDER_START_HOUR={config.REMINDER_START_HOUR}",
        f"REMINDER_END_HOUR={config.REMINDER_END_HOUR}",
        "DASHBOARD_PASSWORD=change_me_before_deploy",
    ]

    token_path = config.get_token_path()
    if token_path.exists():
        token_min = json.dumps(json.loads(token_path.read_text(encoding="utf-8")), separators=(",", ":"))
        lines.append(f"GOOGLE_TOKEN_JSON={token_min}")

    cred_path = config.get_credentials_path()
    if cred_path.exists():
        cred_min = json.dumps(json.loads(cred_path.read_text(encoding="utf-8")), separators=(",", ":"))
        lines.append(f"GOOGLE_CREDENTIALS_JSON={cred_min}")

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[SUCCESS] Cloud environment bundle saved to: {out_path}")
    print("   WARNING: This file contains secrets and is gitignored. Paste its variables into your cloud host, then delete it if desired.")


def main():
    parser = argparse.ArgumentParser(description="HabitSync AI - CLI Test Suite")
    parser.add_argument("--check", action="store_true", help="Check system environment status")
    parser.add_argument("--auth", action="store_true", help="Run Google OAuth2 authentication flow")
    parser.add_argument("--list", action="store_true", help="List pending Google Tasks")
    parser.add_argument("--add", type=str, help="Add a new task with the given title")
    parser.add_argument("--complete", type=str, nargs="?", const="", help="Complete a task by ID (or top task if empty)")
    parser.add_argument("--gemini", type=str, nargs="?", const="Hey, what should I work on today?", help="Test Gemini conversational chat")
    parser.add_argument("--telegram", action="store_true", help="Test Telegram Bot API connection")
    parser.add_argument("--send-reminder", action="store_true", help="Send task reminder via active channel(s)")
    parser.add_argument("--simulate", type=str, help="Simulate an incoming message (e.g. 'done all aws' or 'hi')")
    parser.add_argument("--export-cloud-env", action="store_true", help="Export single-line env vars to .cloud-env.txt for cloud deploy")

    if len(sys.argv) == 1:
        check_status()
        parser.print_help()
        return

    args = parser.parse_args()

    if args.check:
        check_status()
    if args.auth:
        test_auth()
    if args.list:
        test_list_tasks()
    if args.add:
        test_add_task(args.add)
    if args.complete is not None:
        test_complete_task(args.complete if args.complete != "" else None)
    if args.gemini:
        test_gemini(args.gemini)
    if args.telegram:
        test_telegram()
    if args.send_reminder:
        test_send_reminder()
    if args.simulate:
        simulate_incoming_message(args.simulate)
    if args.export_cloud_env:
        export_cloud_env()


if __name__ == "__main__":
    main()
