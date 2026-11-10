import asyncio
import base64
import hmac
import json
import logging
import os
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, Request, Form, Response, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from config import config
from google_tasks_service import tasks_service
from gemini_service import gemini_service
from whatsapp_service import whatsapp_service
from telegram_service import telegram_service
from scheduler_service import reminder_scheduler
from task_resolver import resolve_tasks_to_complete, chat_contexts, execute_completion

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("HabitSyncApp")

app = FastAPI(
    title="HabitSync AI - GUI Dashboard",
    description="Interactive Web Interface & Backend for HabitSync AI (Google Tasks + WhatsApp + Telegram + Gemini)",
    version="1.2.0"
)

PUBLIC_PATHS = {"/health", "/webhook/telegram", "/webhook/whatsapp"}


@app.middleware("http")
async def optional_dashboard_auth(request: Request, call_next):
    """Protects dashboard and API endpoints with HTTP Basic Auth if DASHBOARD_PASSWORD is set."""
    expected_pw = config.DASHBOARD_PASSWORD
    if not expected_pw or request.url.path in PUBLIC_PATHS:
        return await call_next(request)

    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
            _, password = decoded.split(":", 1)
            if hmac.compare_digest(password, expected_pw):
                return await call_next(request)
        except Exception:
            pass

    return Response(
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="HabitSync AI Dashboard"'},
        content="Authentication required"
    )


@app.on_event("startup")
async def startup_event():
    """Starts Telegram listener (webhook in cloud if PUBLIC_BASE_URL is set, else polling) and 24/7 scheduler."""
    loop = asyncio.get_running_loop()
    telegram_service.set_loop(loop)
    if config.TELEGRAM_BOT_TOKEN and not config.TELEGRAM_BOT_TOKEN.startswith("your_"):
        telegram_service.start_listener(loop=loop)
    reminder_scheduler.start(loop=loop)


@app.on_event("shutdown")
async def shutdown_event():
    """Stops Telegram polling and background scheduler cleanly on shutdown."""
    telegram_service.stop_polling()
    reminder_scheduler.stop()


@app.get("/health")
def health_check():
    """Lightweight health check and free-tier keep-alive endpoint."""
    return {
        "status": "ok",
        "environment": "cloud" if config.IS_CLOUD else "local",
        "telegram_active": telegram_service.is_polling(),
        "google_tasks_authenticated": tasks_service.is_authenticated(),
        "scheduler": reminder_scheduler.get_status(),
    }

COMPLETION_KEYWORDS = {
    "done", "complete", "completed", "ho gaya", "khatam", "finished", "check", "ho gya", "done!"
}
LIST_KEYWORDS = {
    "tasks", "list", "pending", "all tasks", "task list", "meri tasks"
}


# ============================================================================
# API Models
# ============================================================================
class SettingsPayload(BaseModel):
    gemini_api_key: Optional[str] = None
    gemini_model: Optional[str] = None
    twilio_account_sid: Optional[str] = None
    twilio_auth_token: Optional[str] = None
    twilio_whatsapp_number: Optional[str] = None
    twilio_content_sid: Optional[str] = None
    user_whatsapp_number: Optional[str] = None
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    notification_channel: Optional[str] = None
    reminder_enabled: Optional[bool] = None
    reminder_interval_minutes: Optional[int] = None
    reminder_start_hour: Optional[int] = None
    reminder_end_hour: Optional[int] = None


class TestGeminiPayload(BaseModel):
    gemini_api_key: Optional[str] = None


class TestTwilioPayload(BaseModel):
    twilio_account_sid: Optional[str] = None
    twilio_auth_token: Optional[str] = None
    twilio_whatsapp_number: Optional[str] = None
    twilio_content_sid: Optional[str] = None
    user_whatsapp_number: Optional[str] = None


class TestTelegramPayload(BaseModel):
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None


class CreateTaskPayload(BaseModel):
    title: str
    notes: Optional[str] = None
    due: Optional[str] = None


class ChatPayload(BaseModel):
    message: str


# ============================================================================
# Backend REST Endpoints
# ============================================================================
@app.get("/api/status")
def get_status():
    """Returns status of all integrations and settings."""
    val = config.validate()
    def mask(s: str) -> str:
        if not s or s.startswith("your_"):
            return ""
        if len(s) <= 8:
            return "***"
        return s[:4] + "..." + s[-4:]

    return {
        "status": val,
        "is_authenticated_google": tasks_service.is_authenticated(),
        "scheduler": reminder_scheduler.get_status(),
        "credentials": {
            "gemini_api_key_masked": mask(config.GEMINI_API_KEY),
            "gemini_model": config.GEMINI_MODEL,
            "twilio_account_sid_masked": mask(config.TWILIO_ACCOUNT_SID),
            "twilio_auth_token_masked": mask(config.TWILIO_AUTH_TOKEN),
            "twilio_whatsapp_number": config.TWILIO_WHATSAPP_NUMBER,
            "twilio_content_sid": config.TWILIO_CONTENT_SID,
            "user_whatsapp_number": config.USER_WHATSAPP_NUMBER,
            "telegram_bot_token_masked": mask(config.TELEGRAM_BOT_TOKEN),
            "telegram_chat_id": config.TELEGRAM_CHAT_ID,
            "notification_channel": config.NOTIFICATION_CHANNEL,
            "telegram_is_polling": telegram_service.is_polling(),
            "telegram_webhook_url": f"{config.PUBLIC_BASE_URL}/webhook/telegram" if config.PUBLIC_BASE_URL else "",
            "reminder_enabled": config.REMINDER_ENABLED,
            "reminder_interval_minutes": config.REMINDER_INTERVAL_MINUTES,
            "reminder_start_hour": config.REMINDER_START_HOUR,
            "reminder_end_hour": config.REMINDER_END_HOUR,
            "has_credentials_json": config.has_google_credentials(),
            "has_token_json": config.has_google_token(),
        }
    }


@app.post("/api/settings")
def update_settings(payload: SettingsPayload):
    """Updates .env/runtime env and reloads config in memory."""
    updates = {}
    if payload.gemini_api_key is not None and payload.gemini_api_key.strip():
        updates["GEMINI_API_KEY"] = payload.gemini_api_key.strip()
    if payload.gemini_model is not None and payload.gemini_model.strip():
        updates["GEMINI_MODEL"] = payload.gemini_model.strip()
    if payload.twilio_account_sid is not None and payload.twilio_account_sid.strip():
        updates["TWILIO_ACCOUNT_SID"] = payload.twilio_account_sid.strip()
    if payload.twilio_auth_token is not None and payload.twilio_auth_token.strip():
        updates["TWILIO_AUTH_TOKEN"] = payload.twilio_auth_token.strip()
    if payload.twilio_whatsapp_number is not None and payload.twilio_whatsapp_number.strip():
        updates["TWILIO_WHATSAPP_NUMBER"] = payload.twilio_whatsapp_number.strip()
    if payload.twilio_content_sid is not None and payload.twilio_content_sid.strip():
        updates["TWILIO_CONTENT_SID"] = payload.twilio_content_sid.strip()
    if payload.user_whatsapp_number is not None and payload.user_whatsapp_number.strip():
        updates["USER_WHATSAPP_NUMBER"] = payload.user_whatsapp_number.strip()
    if payload.telegram_bot_token is not None and payload.telegram_bot_token.strip():
        updates["TELEGRAM_BOT_TOKEN"] = payload.telegram_bot_token.strip()
    if payload.telegram_chat_id is not None and payload.telegram_chat_id.strip():
        updates["TELEGRAM_CHAT_ID"] = payload.telegram_chat_id.strip()
    if payload.notification_channel is not None and payload.notification_channel.strip():
        updates["NOTIFICATION_CHANNEL"] = payload.notification_channel.strip().lower()
    if payload.reminder_enabled is not None:
        updates["REMINDER_ENABLED"] = "true" if payload.reminder_enabled else "false"
    if payload.reminder_interval_minutes is not None:
        updates["REMINDER_INTERVAL_MINUTES"] = str(max(15, payload.reminder_interval_minutes))
    if payload.reminder_start_hour is not None:
        updates["REMINDER_START_HOUR"] = str(max(0, min(23, payload.reminder_start_hour)))
    if payload.reminder_end_hour is not None:
        updates["REMINDER_END_HOUR"] = str(max(0, min(23, payload.reminder_end_hour)))

    config.save_settings(updates)

    # Start or restart listener if Telegram token was configured or updated
    if updates.get("TELEGRAM_BOT_TOKEN"):
        telegram_service.stop_polling()
        telegram_service.start_listener()
    elif config.TELEGRAM_BOT_TOKEN and not telegram_service.is_polling():
        telegram_service.start_listener()

    return {"status": "success", "message": "Settings saved successfully!"}


@app.post("/api/upload-credentials")
async def upload_credentials(file: UploadFile = File(...)):
    """Uploads and saves credentials.json for Google OAuth."""
    try:
        content = await file.read()
        content_str = content.decode("utf-8")
        tasks_service.save_credentials_file(content_str)
        return {"status": "success", "message": "credentials.json uploaded and verified successfully!"}
    except Exception as e:
        logger.error(f"Upload error: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/auth/google")
def auth_google():
    """Triggers the browser Google OAuth flow."""
    try:
        res = tasks_service.trigger_oauth_flow_async()
        return res
    except Exception as e:
        logger.error(f"Google auth error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/test/gemini")
def test_gemini(payload: Optional[TestGeminiPayload] = None):
    """Tests Gemini connection with a quick prompt (auto-saves key if supplied)."""
    if payload and payload.gemini_api_key and payload.gemini_api_key.strip():
        config.save_settings({"GEMINI_API_KEY": payload.gemini_api_key.strip()})

    try:
        reply = gemini_service.test_connection()
        return {"status": "success", "response": reply}
    except Exception as e:
        logger.error(f"Gemini test failed: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/test/twilio")
def test_twilio(payload: Optional[TestTwilioPayload] = None):
    """Sends a test WhatsApp message (auto-saves parameters if provided)."""
    updates = {}
    if payload:
        if payload.twilio_account_sid and payload.twilio_account_sid.strip():
            updates["TWILIO_ACCOUNT_SID"] = payload.twilio_account_sid.strip()
        if payload.twilio_auth_token and payload.twilio_auth_token.strip():
            updates["TWILIO_AUTH_TOKEN"] = payload.twilio_auth_token.strip()
        if payload.twilio_whatsapp_number and payload.twilio_whatsapp_number.strip():
            updates["TWILIO_WHATSAPP_NUMBER"] = payload.twilio_whatsapp_number.strip()
        if payload.twilio_content_sid and payload.twilio_content_sid.strip():
            updates["TWILIO_CONTENT_SID"] = payload.twilio_content_sid.strip()
        if payload.user_whatsapp_number and payload.user_whatsapp_number.strip():
            updates["USER_WHATSAPP_NUMBER"] = payload.user_whatsapp_number.strip()
        if updates:
            config.save_settings(updates)

    if not config.USER_WHATSAPP_NUMBER:
        raise HTTPException(status_code=400, detail="Target User WhatsApp Number is not configured!")

    try:
        sid = whatsapp_service.test_connection(config.USER_WHATSAPP_NUMBER)
        return {"status": "success", "message_sid": sid, "message": "Test WhatsApp message sent!"}
    except Exception as e:
        logger.error(f"Twilio test failed: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/test/telegram")
def test_telegram(payload: Optional[TestTelegramPayload] = None):
    """Verifies Telegram Bot Token and sends a test ping if Chat ID is configured."""
    updates = {}
    if payload:
        if payload.telegram_bot_token and payload.telegram_bot_token.strip():
            updates["TELEGRAM_BOT_TOKEN"] = payload.telegram_bot_token.strip()
        if payload.telegram_chat_id and payload.telegram_chat_id.strip():
            updates["TELEGRAM_CHAT_ID"] = payload.telegram_chat_id.strip()
        if updates:
            config.save_settings(updates)

    try:
        bot_info = telegram_service.verify_token()
        # Try detecting chat ID from any pending /start updates before polling consumes them
        if not config.TELEGRAM_CHAT_ID:
            telegram_service.detect_chat_id_from_updates()

        # Start webhook (if PUBLIC_BASE_URL is set) or long-polling loop
        telegram_service.start_listener()

        msg_sent = False
        if config.TELEGRAM_CHAT_ID:
            test_text = (
                f"👋 <b>HabitSync AI Test</b>\n\n"
                f"Telegram integration with <b>@{bot_info.get('username')}</b> is working! 🚀\n\n"
                f"Reply <code>done</code> or <code>/tasks</code> anytime!"
            )
            telegram_service.send_message(test_text)
            msg_sent = True

        return {
            "status": "success",
            "bot": bot_info,
            "chat_id": config.TELEGRAM_CHAT_ID,
            "message_sent": msg_sent,
            "message": (
                f"Connected to @{bot_info.get('username')}! Test message sent to your Telegram!"
                if msg_sent
                else f"Connected to @{bot_info.get('username')}! Open Telegram, search @{bot_info.get('username')}, and click Start."
            )
        }
    except Exception as e:
        logger.error(f"Telegram test failed: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/tasks")
def list_tasks():
    """Fetches pending tasks from Google Tasks."""
    if not tasks_service.is_authenticated():
        return {"status": "unauthenticated", "tasks": []}
    try:
        tasks = tasks_service.get_pending_tasks()
        return {"status": "success", "count": len(tasks), "tasks": tasks}
    except Exception as e:
        logger.error(f"Error fetching tasks: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/tasks")
def create_task(payload: CreateTaskPayload):
    """Creates a new task in Google Tasks."""
    if not tasks_service.is_authenticated():
        raise HTTPException(status_code=400, detail="Google Tasks is not authenticated!")
    try:
        task = tasks_service.create_task(title=payload.title, notes=payload.notes, due=payload.due)
        return {"status": "success", "task": task}
    except Exception as e:
        logger.error(f"Error creating task: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/tasks/{task_id}/complete")
def complete_task_endpoint(task_id: str):
    """Completes a task in Google Tasks."""
    try:
        task = tasks_service.complete_task(task_id)
        return {"status": "success", "task": task}
    except Exception as e:
        logger.error(f"Error completing task: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/chat")
def chat_with_bot(payload: ChatPayload):
    """Sends a message to the AI assistant with tasks context and intelligent task completion."""
    user_msg = payload.message.strip()
    normalized = user_msg.lower()
    session_id = "web"
    last_assistant_msg = chat_contexts.get(session_id, "")

    try:
        pending_tasks = tasks_service.get_pending_tasks() if tasks_service.is_authenticated() else []
    except Exception:
        pending_tasks = []

    # 1. User wants to view task list
    if any(k in normalized for k in LIST_KEYWORDS):
        reply = whatsapp_service.format_reminder_text(pending_tasks)
        chat_contexts[session_id] = reply
        return {"action": "list", "response": reply}

    # 2. Intelligent task completion (resolves by name, keywords, 'all <kw>', index, or context)
    matched_tasks, reason = resolve_tasks_to_complete(user_msg, pending_tasks, last_assistant_msg)
    if matched_tasks:
        reply = execute_completion(matched_tasks, pending_tasks)
        chat_contexts[session_id] = reply
        return {
            "action": "completed",
            "response": reply,
            "matched_tasks": [t["title"] for t in matched_tasks],
            "reason": reason
        }

    # 3. Conversational coaching via Gemini Flash
    try:
        reply = gemini_service.chat_with_tasks(user_msg, pending_tasks)
        chat_contexts[session_id] = reply
        return {"action": "chat", "response": reply}
    except Exception as e:
        return {"action": "error", "response": f"Gemini Error: {e}"}


@app.post("/api/send-reminder")
def send_reminder():
    """Triggers reminder dispatch based on active channel selection (telegram, whatsapp, or both)."""
    channel = config.NOTIFICATION_CHANNEL
    pending = tasks_service.get_pending_tasks() if tasks_service.is_authenticated() else []
    dispatches = {}

    # WhatsApp dispatch
    if channel in ["whatsapp", "both"]:
        if config.USER_WHATSAPP_NUMBER:
            try:
                sid = whatsapp_service.send_task_reminder(config.USER_WHATSAPP_NUMBER, pending)
                dispatches["whatsapp"] = {"status": "sent", "sid": sid}
            except Exception as e:
                dispatches["whatsapp"] = {"status": "error", "error": str(e)}
        else:
            dispatches["whatsapp"] = {"status": "skipped", "reason": "No WhatsApp number configured"}

    # Telegram dispatch
    if channel in ["telegram", "both"]:
        if config.TELEGRAM_BOT_TOKEN and not config.TELEGRAM_CHAT_ID:
            telegram_service.detect_chat_id_from_updates()

        if config.TELEGRAM_CHAT_ID and config.TELEGRAM_BOT_TOKEN:
            try:
                res = telegram_service.send_task_reminder(pending_tasks=pending)
                dispatches["telegram"] = {"status": "sent", "msg_id": res.get("message_id")}
            except Exception as e:
                dispatches["telegram"] = {"status": "error", "error": str(e)}
        else:
            dispatches["telegram"] = {"status": "skipped", "reason": "Telegram Bot Token or Chat ID missing"}

    return {
        "status": "success",
        "channel": channel,
        "tasks_count": len(pending),
        "dispatches": dispatches
    }


@app.post("/webhook/telegram")
async def telegram_webhook(request: Request):
    """Telegram Bot HTTPS Webhook (used automatically on cloud hosts with PUBLIC_BASE_URL)."""
    if config.TELEGRAM_WEBHOOK_SECRET:
        header_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(header_secret, config.TELEGRAM_WEBHOOK_SECRET):
            raise HTTPException(status_code=403, detail="Invalid Telegram webhook secret token")

    update = await request.json()
    await telegram_service.handle_webhook_update(update)
    return {"ok": True}


@app.post("/webhook/whatsapp")
async def whatsapp_webhook(
    From: str = Form(...),
    Body: str = Form(...),
    ProfileName: Optional[str] = Form(None)
):
    """Twilio WhatsApp Inbound Webhook with intelligent task resolution."""
    user_text = Body.strip()
    normalized = user_text.lower()
    session_id = From
    last_assistant_msg = chat_contexts.get(session_id, "")
    logger.info(f"Incoming WhatsApp message from {From}: '{user_text}'")

    try:
        pending_tasks = tasks_service.get_pending_tasks() if tasks_service.is_authenticated() else []
    except Exception as e:
        logger.error(f"Failed to fetch tasks: {e}")
        pending_tasks = []

    # 1. User wants to view task list
    if any(k in normalized for k in LIST_KEYWORDS):
        response_text = whatsapp_service.format_reminder_text(pending_tasks)
        chat_contexts[session_id] = response_text
    else:
        # 2. Intelligent task completion
        matched_tasks, reason = resolve_tasks_to_complete(user_text, pending_tasks, last_assistant_msg)
        if matched_tasks:
            response_text = execute_completion(matched_tasks, pending_tasks)
            chat_contexts[session_id] = response_text
        else:
            # 3. Conversational coaching via Gemini Flash
            try:
                response_text = gemini_service.chat_with_tasks(user_text, pending_tasks)
                chat_contexts[session_id] = response_text
            except Exception as e:
                response_text = "I couldn't reach Gemini right now. Reply *done* or tell me which task you finished!"

    twiml_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Message>{response_text}</Message>
</Response>"""
    return Response(content=twiml_response, media_type="application/xml")


# ============================================================================
# Frontend GUI Dashboard with Auto-Saving Tests & Interactive Setup Guides
# ============================================================================
@app.get("/", response_class=HTMLResponse)
def index():
    return """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>HabitSync AI - Personal Assistant Dashboard</title>
    <!-- Tailwind CSS -->
    <script src="https://cdn.tailwindcss.com"></script>
    <!-- Lucide Icons -->
    <script src="https://unpkg.com/lucide@latest"></script>
    <script>
        tailwind.config = {
            darkMode: 'class',
            theme: {
                extend: {
                    colors: {
                        brand: { 500: '#38bdf8', 600: '#0284c7', 700: '#0369a1' },
                        dark: { 900: '#0b0f19', 800: '#111827', 700: '#1f2937', 600: '#374151' }
                    }
                }
            }
        }
    </script>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap');
        body { font-family: 'Plus Jakarta Sans', sans-serif; }
        .glass-panel { background: rgba(17, 24, 39, 0.85); backdrop-filter: blur(12px); border: 1px solid rgba(255, 255, 255, 0.08); }
        .tab-btn.active { background: #0284c7; color: white; }
        .guide-box { background: rgba(30, 41, 59, 0.7); border: 1px solid rgba(56, 189, 248, 0.2); }
        .step-num { background: #0284c7; color: white; width: 22px; height: 22px; border-radius: 9999px; display: inline-flex; align-items: center; justify-content: center; font-size: 11px; font-weight: bold; flex-shrink: 0; }
    </style>
</head>
<body class="bg-dark-900 text-slate-100 min-h-screen flex flex-col">

    <!-- Top Navigation Header -->
    <header class="border-b border-slate-800 bg-dark-800/80 sticky top-0 z-50 backdrop-blur-md">
        <div class="max-w-7xl mx-auto px-6 h-16 flex items-center justify-between">
            <div class="flex items-center space-x-3">
                <div class="w-10 h-10 rounded-xl bg-gradient-to-tr from-sky-500 to-indigo-600 flex items-center justify-center shadow-lg shadow-sky-500/20">
                    <i data-lucide="sparkles" class="w-5 h-5 text-white"></i>
                </div>
                <div>
                    <h1 class="font-bold text-lg leading-tight flex items-center gap-2">
                        HabitSync AI <span class="text-xs px-2 py-0.5 rounded-full bg-sky-500/20 text-sky-400 font-semibold border border-sky-500/30">Phase 1 GUI</span>
                    </h1>
                    <p class="text-xs text-slate-400">Google Tasks • Telegram &amp; WhatsApp • Gemini Flash</p>
                </div>
            </div>

            <!-- Quick Status Badges & Guide Button -->
            <div class="flex items-center gap-2.5">
                <button onclick="openHelpModal()" class="hidden sm:flex items-center gap-1.5 px-3 py-1.5 rounded-xl bg-sky-500/10 hover:bg-sky-500/20 text-sky-300 border border-sky-500/30 text-xs font-semibold transition">
                    <i data-lucide="book-open" class="w-3.5 h-3.5"></i> Setup Guide
                </button>
                <div id="badge-google" class="px-2.5 py-1 rounded-full bg-slate-800 border border-slate-700 flex items-center gap-1.5 text-xs text-slate-400">
                    <span class="w-2 h-2 rounded-full bg-slate-500"></span> Tasks
                </div>
                <div id="badge-gemini" class="px-2.5 py-1 rounded-full bg-slate-800 border border-slate-700 flex items-center gap-1.5 text-xs text-slate-400">
                    <span class="w-2 h-2 rounded-full bg-slate-500"></span> Gemini
                </div>
                <div id="badge-telegram" class="px-2.5 py-1 rounded-full bg-slate-800 border border-slate-700 flex items-center gap-1.5 text-xs text-slate-400">
                    <span class="w-2 h-2 rounded-full bg-slate-500"></span> Telegram
                </div>
                <div id="badge-twilio" class="px-2.5 py-1 rounded-full bg-slate-800 border border-slate-700 flex items-center gap-1.5 text-xs text-slate-400">
                    <span class="w-2 h-2 rounded-full bg-slate-500"></span> WhatsApp
                </div>
            </div>
        </div>
    </header>

    <!-- Main Container -->
    <main class="max-w-7xl mx-auto px-6 py-8 flex-1 w-full flex flex-col md:flex-row gap-8">

        <!-- Sidebar Tabs -->
        <aside class="w-full md:w-64 flex flex-row md:flex-col gap-2 shrink-0">
            <button onclick="switchTab('settings')" id="tab-btn-settings" class="tab-btn active w-full flex items-center gap-3 px-4 py-3 rounded-xl font-medium text-sm transition-all text-slate-300 hover:bg-slate-800">
                <i data-lucide="key-round" class="w-4 h-4"></i>
                <span>API & Setup</span>
            </button>
            <button onclick="switchTab('tasks')" id="tab-btn-tasks" class="tab-btn w-full flex items-center gap-3 px-4 py-3 rounded-xl font-medium text-sm transition-all text-slate-300 hover:bg-slate-800">
                <i data-lucide="check-square" class="w-4 h-4"></i>
                <span>Google Tasks Live</span>
                <span id="tasks-count-pill" class="ml-auto text-xs px-2 py-0.5 rounded-full bg-slate-700 text-slate-300 hidden">0</span>
            </button>
            <button onclick="switchTab('chat')" id="tab-btn-chat" class="tab-btn w-full flex items-center gap-3 px-4 py-3 rounded-xl font-medium text-sm transition-all text-slate-300 hover:bg-slate-800">
                <i data-lucide="message-square" class="w-4 h-4"></i>
                <span>AI Chat & Simulator</span>
            </button>
            
            <div class="mt-6 pt-6 border-t border-slate-800 hidden md:block space-y-3">
                <button onclick="openHelpModal()" class="w-full flex items-center justify-center gap-2 bg-slate-800 hover:bg-slate-700 border border-slate-700 text-sky-400 font-semibold px-4 py-2.5 rounded-xl text-xs transition">
                    <i data-lucide="help-circle" class="w-4 h-4"></i> How to Get Keys Guide
                </button>
                <button onclick="triggerReminderNow()" class="w-full flex items-center justify-center gap-2 bg-gradient-to-r from-sky-600 to-emerald-600 hover:from-sky-500 hover:to-emerald-500 text-white font-semibold px-4 py-2.5 rounded-xl text-xs transition shadow-lg shadow-sky-600/20">
                    <i data-lucide="send" class="w-3.5 h-3.5"></i> Send Nudge Now
                </button>
            </div>
        </aside>

        <!-- Tab Content Area -->
        <section class="flex-1 w-full">

            <!-- ============================================================ -->
            <!-- TAB 1: API & CREDENTIALS SETUP                               -->
            <!-- ============================================================ -->
            <div id="tab-settings" class="space-y-6">
                <div class="flex items-center justify-between">
                    <div>
                        <h2 class="text-xl font-bold text-white flex items-center gap-2">
                            <i data-lucide="sliders" class="w-5 h-5 text-sky-400"></i> Integrations & API Credentials
                        </h2>
                        <p class="text-sm text-slate-400">Follow the easy step-by-step guides below to connect each service.</p>
                    </div>
                </div>

                <!-- Active Notification Channel Selector Card -->
                <div class="glass-panel p-5 rounded-2xl space-y-3 border border-sky-500/20 bg-gradient-to-r from-sky-950/40 via-dark-800 to-emerald-950/30">
                    <div class="flex items-center justify-between">
                        <div class="flex items-center gap-3">
                            <div class="p-2 rounded-xl bg-sky-500/10 text-sky-400">
                                <i data-lucide="radio" class="w-5 h-5"></i>
                            </div>
                            <div>
                                <h3 class="font-bold text-white text-sm">Active Habit Nudge Channel</h3>
                                <p class="text-xs text-slate-400">Choose which platform HabitSync AI uses for task nudges and habit accountability</p>
                            </div>
                        </div>
                        <span id="active-channel-pill" class="text-xs px-3 py-1 rounded-full font-bold bg-sky-500/20 text-sky-300 border border-sky-500/30">Both Active</span>
                    </div>
                    <div class="grid grid-cols-1 sm:grid-cols-3 gap-3 pt-1">
                        <label class="cursor-pointer flex items-center gap-3 p-3.5 rounded-xl border border-slate-700 bg-dark-800/80 hover:border-sky-500 transition has-[:checked]:border-sky-500 has-[:checked]:bg-sky-500/10">
                            <input type="radio" name="notification_channel" value="telegram" onchange="updateChannelBadge('telegram')" class="text-sky-500 focus:ring-0">
                            <div>
                                <div class="text-xs font-bold text-white flex items-center gap-1.5">
                                    <i data-lucide="send" class="w-3.5 h-3.5 text-sky-400"></i> Telegram (Recommended)
                                </div>
                                <div class="text-[11px] text-slate-400">100% Free, instant 2-way bot, zero sandbox</div>
                            </div>
                        </label>
                        <label class="cursor-pointer flex items-center gap-3 p-3.5 rounded-xl border border-slate-700 bg-dark-800/80 hover:border-emerald-500 transition has-[:checked]:border-emerald-500 has-[:checked]:bg-emerald-500/10">
                            <input type="radio" name="notification_channel" value="whatsapp" onchange="updateChannelBadge('whatsapp')" class="text-emerald-500 focus:ring-0">
                            <div>
                                <div class="text-xs font-bold text-white flex items-center gap-1.5">
                                    <i data-lucide="message-circle" class="w-3.5 h-3.5 text-emerald-400"></i> WhatsApp
                                </div>
                                <div class="text-[11px] text-slate-400">Twilio WhatsApp Sandbox</div>
                            </div>
                        </label>
                        <label class="cursor-pointer flex items-center gap-3 p-3.5 rounded-xl border border-slate-700 bg-dark-800/80 hover:border-purple-500 transition has-[:checked]:border-purple-500 has-[:checked]:bg-purple-500/10">
                            <input type="radio" name="notification_channel" value="both" checked onchange="updateChannelBadge('both')" class="text-purple-500 focus:ring-0">
                            <div>
                                <div class="text-xs font-bold text-white flex items-center gap-1.5">
                                    <i data-lucide="zap" class="w-3.5 h-3.5 text-purple-400"></i> Both Platforms
                                </div>
                                <div class="text-[11px] text-slate-400">Dual sync across Telegram &amp; WhatsApp</div>
                            </div>
                        </label>
                    </div>
                </div>

                <!-- Google Tasks Card -->
                <div class="glass-panel p-6 rounded-2xl space-y-4">
                    <div class="flex items-center justify-between border-b border-slate-800 pb-4">
                        <div class="flex items-center gap-3">
                            <div class="p-2.5 rounded-xl bg-blue-500/10 text-blue-400">
                                <i data-lucide="check-circle-2" class="w-6 h-6"></i>
                            </div>
                            <div>
                                <h3 class="font-semibold text-white">1. Google Tasks Authorization</h3>
                                <p class="text-xs text-slate-400">Syncs your tasks unidirectionally with Google Tasks</p>
                            </div>
                        </div>
                        <div class="flex items-center gap-2">
                            <button onclick="toggleGuide('guide-tasks')" class="text-xs px-2.5 py-1 rounded-lg bg-sky-500/10 hover:bg-sky-500/20 text-sky-400 border border-sky-500/20 font-medium flex items-center gap-1 transition">
                                <i data-lucide="book-open" class="w-3 h-3"></i> Easy Steps Guide
                            </button>
                            <span id="google-status-pill" class="text-xs px-3 py-1 rounded-full font-medium bg-slate-800 text-slate-400">Checking...</span>
                        </div>
                    </div>

                    <!-- Step-by-Step Guide for Google Tasks (Collapsible) -->
                    <div id="guide-tasks" class="guide-box p-4 rounded-xl space-y-3 hidden">
                        <h4 class="text-xs font-bold text-sky-300 flex items-center gap-1.5 uppercase tracking-wide">
                            <i data-lucide="info" class="w-4 h-4"></i> How to get credentials.json from Google Cloud (Free - 3 Mins)
                        </h4>
                        <ol class="text-xs text-slate-300 space-y-2 leading-relaxed">
                            <li class="flex items-start gap-2">
                                <span class="step-num">1</span>
                                <div>Open <a href="https://console.cloud.google.com" target="_blank" class="text-sky-400 underline font-semibold">Google Cloud Console</a> and create or select a project.</div>
                            </li>
                            <li class="flex items-start gap-2">
                                <span class="step-num">2</span>
                                <div>Go to <b>APIs & Services</b> &rarr; <b>Library</b>, search for <b>"Google Tasks API"</b>, and click <b>Enable</b>.</div>
                            </li>
                            <li class="flex items-start gap-2">
                                <span class="step-num">3</span>
                                <div>Go to <b>APIs & Services</b> &rarr; <b>OAuth consent screen</b>:
                                    <ul class="list-disc ml-5 mt-1 space-y-0.5 text-slate-400">
                                        <li>Choose <b>External</b> &rarr; App Name: <code class="text-sky-300">HabitSync</code> &rarr; Enter your email.</li>
                                        <li>Under <b>Test users</b>, add your own Gmail address (Crucial! Otherwise access is blocked).</li>
                                    </ul>
                                </div>
                            </li>
                            <li class="flex items-start gap-2">
                                <span class="step-num">4</span>
                                <div>Go to <b>APIs & Services</b> &rarr; <b>Credentials</b> &rarr; Click <b>Create Credentials</b> &rarr; <b>OAuth client ID</b>:
                                    <ul class="list-disc ml-5 mt-1 space-y-0.5 text-slate-400">
                                        <li>Application type: <b>Desktop app</b>.</li>
                                        <li>Name: <code class="text-sky-300">HabitSync Desktop</code> (Leave 'AI-powered agent' UNCHECKED).</li>
                                        <li>Click <b>Create</b>.</li>
                                    </ul>
                                </div>
                            </li>
                            <li class="flex items-start gap-2">
                                <span class="step-num">5</span>
                                <div>Click the download icon ⬇️ to download your JSON file. Then click the <b>Upload button</b> below!</div>
                            </li>
                        </ol>
                    </div>

                    <div class="grid md:grid-cols-2 gap-4 pt-1">
                        <!-- Step A: Upload credentials.json -->
                        <div class="bg-dark-800/60 p-4 rounded-xl border border-slate-800 space-y-3">
                            <div class="flex items-center justify-between">
                                <span class="text-xs font-semibold uppercase tracking-wider text-slate-400">Step A: Upload File</span>
                                <span id="cred-file-status" class="text-xs text-slate-400">Not Uploaded</span>
                            </div>
                            <p class="text-xs text-slate-400 leading-relaxed">
                                Upload your downloaded OAuth client JSON. It will automatically be saved as <code class="text-sky-300 font-mono">credentials.json</code>.
                            </p>
                            <label class="cursor-pointer flex items-center justify-center gap-2 bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 rounded-lg px-3 py-2 text-xs font-medium transition">
                                <i data-lucide="upload" class="w-3.5 h-3.5"></i>
                                <span>Select & Upload credentials.json</span>
                                <input type="file" id="cred-file-input" accept=".json" class="hidden" onchange="uploadCredentialsFile(event)">
                            </label>
                        </div>

                        <!-- Step B: Authorize with Google -->
                        <div class="bg-dark-800/60 p-4 rounded-xl border border-slate-800 space-y-3 flex flex-col justify-between">
                            <div>
                                <div class="flex items-center justify-between">
                                    <span class="text-xs font-semibold uppercase tracking-wider text-slate-400">Step B: 1-Click Login</span>
                                    <span id="auth-token-status" class="text-xs text-slate-400">Pending</span>
                                </div>
                                <p class="text-xs text-slate-400 leading-relaxed mt-2">
                                    Clicking opens Google sign-in in your browser to grant Tasks read/write permissions.
                                </p>
                            </div>
                            <button id="btn-google-auth" onclick="startGoogleAuth()" class="w-full flex items-center justify-center gap-2 bg-blue-600 hover:bg-blue-500 text-white font-semibold px-4 py-2.5 rounded-lg text-xs transition">
                                <i data-lucide="log-in" class="w-3.5 h-3.5"></i>
                                Connect Google Account
                            </button>
                        </div>
                    </div>
                </div>

                <!-- Google Gemini Card -->
                <div class="glass-panel p-6 rounded-2xl space-y-4">
                    <div class="flex items-center justify-between border-b border-slate-800 pb-4">
                        <div class="flex items-center gap-3">
                            <div class="p-2.5 rounded-xl bg-purple-500/10 text-purple-400">
                                <i data-lucide="bot" class="w-6 h-6"></i>
                            </div>
                            <div>
                                <h3 class="font-semibold text-white">2. Google Gemini API (BYOK Model)</h3>
                                <p class="text-xs text-slate-400">Conversational intelligence and habit accountability buddy</p>
                            </div>
                        </div>
                        <button onclick="toggleGuide('guide-gemini')" class="text-xs px-2.5 py-1 rounded-lg bg-sky-500/10 hover:bg-sky-500/20 text-sky-400 border border-sky-500/20 font-medium flex items-center gap-1 transition">
                            <i data-lucide="book-open" class="w-3 h-3"></i> Easy Steps Guide
                        </button>
                    </div>

                    <!-- Step-by-Step Guide for Gemini (Collapsible) -->
                    <div id="guide-gemini" class="guide-box p-4 rounded-xl space-y-3 hidden">
                        <h4 class="text-xs font-bold text-sky-300 flex items-center gap-1.5 uppercase tracking-wide">
                            <i data-lucide="info" class="w-4 h-4"></i> How to get a 100% Free Gemini API Key (Takes 1 Minute)
                        </h4>
                        <ol class="text-xs text-slate-300 space-y-2 leading-relaxed">
                            <li class="flex items-start gap-2">
                                <span class="step-num">1</span>
                                <div>Open <a href="https://aistudio.google.com" target="_blank" class="text-sky-400 underline font-semibold">Google AI Studio (aistudio.google.com)</a> and sign in with your Google account.</div>
                            </li>
                            <li class="flex items-start gap-2">
                                <span class="step-num">2</span>
                                <div>Click the blue <b>"Get API key"</b> button on the top-left sidebar.</div>
                            </li>
                            <li class="flex items-start gap-2">
                                <span class="step-num">3</span>
                                <div>Click <b>"Create API key"</b> &rarr; Choose or create any project.</div>
                            </li>
                            <li class="flex items-start gap-2">
                                <span class="step-num">4</span>
                                <div>Copy the key string (starts with <code class="text-sky-300 font-mono">AIzaSy...</code>) and paste it into the box below!</div>
                            </li>
                        </ol>
                    </div>

                    <div class="space-y-3">
                        <label class="block text-xs font-semibold uppercase tracking-wider text-slate-400">Gemini API Key</label>
                        <div class="flex gap-2">
                            <div class="relative flex-1">
                                <input type="password" id="input-gemini-key" placeholder="Paste your AIzaSy... key here" class="w-full bg-dark-800 border border-slate-700 rounded-xl px-4 py-2.5 text-sm text-white focus:outline-none focus:border-sky-500 font-mono">
                                <button onclick="toggleVisibility('input-gemini-key')" class="absolute right-3 top-3 text-slate-400 hover:text-white">
                                    <i data-lucide="eye" class="w-4 h-4"></i>
                                </button>
                            </div>
                            <button onclick="testGeminiKey()" class="px-4 py-2.5 bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 rounded-xl text-xs font-semibold flex items-center gap-1.5 transition">
                                <i data-lucide="zap" class="w-3.5 h-3.5 text-amber-400"></i> Test Key
                            </button>
                        </div>
                    </div>
                </div>

                <!-- Telegram Bot Card (NEW) -->
                <div class="glass-panel p-6 rounded-2xl space-y-4">
                    <div class="flex items-center justify-between border-b border-slate-800 pb-4">
                        <div class="flex items-center gap-3">
                            <div class="p-2.5 rounded-xl bg-sky-500/10 text-sky-400">
                                <i data-lucide="send" class="w-6 h-6"></i>
                            </div>
                            <div>
                                <h3 class="font-semibold text-white">3. Telegram Bot Integration (Free &amp; Unlimited)</h3>
                                <p class="text-xs text-slate-400">Instant 2-way accountability buddy, no sandbox codes, no 24-hr timeouts</p>
                            </div>
                        </div>
                        <button onclick="toggleGuide('guide-telegram')" class="text-xs px-2.5 py-1 rounded-lg bg-sky-500/10 hover:bg-sky-500/20 text-sky-400 border border-sky-500/20 font-medium flex items-center gap-1 transition">
                            <i data-lucide="book-open" class="w-3 h-3"></i> Easy Steps Guide
                        </button>
                    </div>

                    <!-- Step-by-Step Guide for Telegram (Collapsible) -->
                    <div id="guide-telegram" class="guide-box p-4 rounded-xl space-y-3 hidden">
                        <h4 class="text-xs font-bold text-sky-300 flex items-center gap-1.5 uppercase tracking-wide">
                            <i data-lucide="info" class="w-4 h-4"></i> How to create your free Telegram Bot (Takes 30 Seconds)
                        </h4>
                        <ol class="text-xs text-slate-300 space-y-2 leading-relaxed">
                            <li class="flex items-start gap-2">
                                <span class="step-num">1</span>
                                <div>Open Telegram on your phone or web, and open <a href="https://t.me/BotFather" target="_blank" class="text-sky-400 underline font-semibold">@BotFather (t.me/BotFather)</a>.</div>
                            </li>
                            <li class="flex items-start gap-2">
                                <span class="step-num">2</span>
                                <div>Send the message <code>/newbot</code> to @BotFather.</div>
                            </li>
                            <li class="flex items-start gap-2">
                                <span class="step-num">3</span>
                                <div>Enter a display name (e.g., <b>HabitSync AI</b>) and a username ending in <code>bot</code> (e.g., <b>HabitSyncShivanshBot</b>).</div>
                            </li>
                            <li class="flex items-start gap-2">
                                <span class="step-num">4</span>
                                <div>BotFather will give you your <b>HTTP API token</b> (e.g. <code class="text-sky-300 font-mono">7123456789:AAH...</code>). Copy and paste it below!</div>
                            </li>
                            <li class="flex items-start gap-2">
                                <span class="step-num">5</span>
                                <div>Click <b>"Connect &amp; Test Telegram Bot"</b>, then open your bot in Telegram and tap <b>Start</b>! Your Chat ID is auto-detected.</div>
                            </li>
                        </ol>
                    </div>

                    <div class="grid md:grid-cols-2 gap-4">
                        <div class="space-y-1">
                            <label class="block text-xs font-semibold text-slate-400">Telegram Bot Token</label>
                            <div class="relative">
                                <input type="password" id="input-telegram-token" placeholder="Paste 123456:ABC-DEF1234ghIkl-zyx57... from @BotFather" class="w-full bg-dark-800 border border-slate-700 rounded-xl px-4 py-2 text-sm text-white focus:outline-none focus:border-sky-500 font-mono">
                                <button onclick="toggleVisibility('input-telegram-token')" class="absolute right-3 top-2.5 text-slate-400 hover:text-white">
                                    <i data-lucide="eye" class="w-4 h-4"></i>
                                </button>
                            </div>
                        </div>
                        <div class="space-y-1">
                            <div class="flex items-center justify-between">
                                <label class="block text-xs font-semibold text-slate-400">Your Telegram Chat ID</label>
                                <span class="text-[10px] text-sky-400 font-medium">Auto-detected upon sending /start</span>
                            </div>
                            <input type="text" id="input-telegram-chat-id" placeholder="Auto-fills when you message your bot" class="w-full bg-dark-800 border border-slate-700 rounded-xl px-4 py-2 text-sm text-white focus:outline-none focus:border-sky-500 font-mono">
                        </div>
                    </div>

                    <div class="pt-2 flex items-center justify-between">
                        <div id="telegram-bot-badge" class="text-xs text-slate-400 flex items-center gap-1.5">
                            <span class="w-2 h-2 rounded-full bg-slate-500"></span> Bot waiting for token
                        </div>
                        <button onclick="testTelegram()" class="px-4 py-2 bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 rounded-xl text-xs font-semibold flex items-center gap-1.5 transition">
                            <i data-lucide="send" class="w-3.5 h-3.5 text-sky-400"></i> Connect &amp; Test Telegram Bot
                        </button>
                    </div>
                </div>

                <!-- Twilio WhatsApp Card -->
                <div class="glass-panel p-6 rounded-2xl space-y-4">
                    <div class="flex items-center justify-between border-b border-slate-800 pb-4">
                        <div class="flex items-center gap-3">
                            <div class="p-2.5 rounded-xl bg-emerald-500/10 text-emerald-400">
                                <i data-lucide="message-circle" class="w-6 h-6"></i>
                            </div>
                            <div>
                                <h3 class="font-semibold text-white">4. Twilio WhatsApp Sandbox</h3>
                                <p class="text-xs text-slate-400">Sends WhatsApp task reminders and receives "done" replies</p>
                            </div>
                        </div>
                        <button onclick="toggleGuide('guide-twilio')" class="text-xs px-2.5 py-1 rounded-lg bg-sky-500/10 hover:bg-sky-500/20 text-sky-400 border border-sky-500/20 font-medium flex items-center gap-1 transition">
                            <i data-lucide="book-open" class="w-3 h-3"></i> Easy Steps Guide
                        </button>
                    </div>

                    <!-- Step-by-Step Guide for Twilio (Collapsible) -->
                    <div id="guide-twilio" class="guide-box p-4 rounded-xl space-y-3 hidden">
                        <h4 class="text-xs font-bold text-sky-300 flex items-center gap-1.5 uppercase tracking-wide">
                            <i data-lucide="info" class="w-4 h-4"></i> How to set up Twilio WhatsApp Sandbox (Free - 2 Mins)
                        </h4>
                        <ol class="text-xs text-slate-300 space-y-2 leading-relaxed">
                            <li class="flex items-start gap-2">
                                <span class="step-num">1</span>
                                <div>Create a free account at <a href="https://console.twilio.com" target="_blank" class="text-sky-400 underline font-semibold">console.twilio.com</a>.</div>
                            </li>
                            <li class="flex items-start gap-2">
                                <span class="step-num">2</span>
                                <div>On your Twilio Console home page, copy your <b>Account SID</b> and <b>Auth Token</b> and paste them below.</div>
                            </li>
                            <li class="flex items-start gap-2">
                                <span class="step-num">3</span>
                                <div>In the left sidebar, navigate to: <b>Messaging</b> &rarr; <b>Try it out</b> &rarr; <b>Send a WhatsApp message</b>.</div>
                            </li>
                            <li class="flex items-start gap-2">
                                <span class="step-num">4</span>
                                <div>You will see a join code on screen (e.g. <code class="text-emerald-400 font-mono">join apple-banana</code>) and a number: <code class="text-sky-300 font-mono">+1 415 523 8886</code>.</div>
                            </li>
                            <li class="flex items-start gap-2">
                                <span class="step-num">5</span>
                                <div>Open WhatsApp on your mobile phone, and send that exact join code to <code class="text-sky-300 font-mono">+1 415 523 8886</code>. Twilio will reply with a confirmation.</div>
                            </li>
                            <li class="flex items-start gap-2">
                                <span class="step-num">6</span>
                                <div>Enter your phone number below with <code class="text-sky-300 font-mono">whatsapp:+countrycode</code> (e.g. for India: <code class="text-emerald-400 font-mono">whatsapp:+919876543210</code>).</div>
                            </li>
                            <li class="flex items-start gap-2">
                                <span class="step-num">7</span>
                                <div>Click <b>"Save Settings"</b> then click <b>"Send Test Message to My Phone"</b>!</div>
                            </li>
                        </ol>
                    </div>

                    <div class="grid md:grid-cols-2 gap-4">
                        <div class="space-y-1">
                            <label class="block text-xs font-semibold text-slate-400">Twilio Account SID</label>
                            <input type="text" id="input-twilio-sid" placeholder="AC..." class="w-full bg-dark-800 border border-slate-700 rounded-xl px-4 py-2 text-sm text-white focus:outline-none focus:border-sky-500 font-mono">
                        </div>
                        <div class="space-y-1">
                            <label class="block text-xs font-semibold text-slate-400">Twilio Auth Token</label>
                            <div class="relative">
                                <input type="password" id="input-twilio-token" placeholder="••••••••••••" class="w-full bg-dark-800 border border-slate-700 rounded-xl px-4 py-2 text-sm text-white focus:outline-none focus:border-sky-500 font-mono">
                                <button onclick="toggleVisibility('input-twilio-token')" class="absolute right-3 top-2.5 text-slate-400 hover:text-white">
                                    <i data-lucide="eye" class="w-4 h-4"></i>
                                </button>
                            </div>
                        </div>
                        <div class="space-y-1">
                            <label class="block text-xs font-semibold text-slate-400">Twilio Sandbox Number</label>
                            <input type="text" id="input-twilio-number" value="whatsapp:+14155238886" class="w-full bg-dark-800 border border-slate-700 rounded-xl px-4 py-2 text-sm text-white focus:outline-none focus:border-sky-500 font-mono">
                        </div>
                        <div class="space-y-1">
                            <label class="block text-xs font-semibold text-slate-400">Twilio Content SID (Template SID)</label>
                            <input type="text" id="input-twilio-content-sid" value="HXfe5ab5f00277942d4d4200328b4d403c" placeholder="HX..." class="w-full bg-dark-800 border border-slate-700 rounded-xl px-4 py-2 text-sm text-white focus:outline-none focus:border-sky-500 font-mono">
                        </div>
                        <div class="space-y-1 md:col-span-2">
                            <label class="block text-xs font-semibold text-slate-400">Your Personal WhatsApp Number (with country code)</label>
                            <input type="text" id="input-user-number" placeholder="whatsapp:+919876543210" class="w-full bg-dark-800 border border-slate-700 rounded-xl px-4 py-2 text-sm text-white focus:outline-none focus:border-sky-500 font-mono">
                        </div>
                    </div>

                    <div class="pt-2 flex justify-end">
                        <button onclick="testTwilio()" class="px-4 py-2 bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 rounded-xl text-xs font-semibold flex items-center gap-1.5 transition">
                            <i data-lucide="send" class="w-3.5 h-3.5 text-emerald-400"></i> Send Test Message to My Phone
                        </button>
                    </div>
                </div>

                <!-- 24/7 Automated Nudge Scheduler Card -->
                <div class="glass-panel p-6 rounded-2xl space-y-4">
                    <div class="flex items-center justify-between border-b border-slate-800 pb-4">
                        <div class="flex items-center gap-3">
                            <div class="p-2.5 rounded-xl bg-amber-500/10 text-amber-400">
                                <i data-lucide="clock" class="w-6 h-6"></i>
                            </div>
                            <div>
                                <h3 class="font-semibold text-white">5. 24/7 Automatic Task Nudge Scheduler</h3>
                                <p class="text-xs text-slate-400">Automatically checks pending Google Tasks and pings your active channel</p>
                            </div>
                        </div>
                        <label class="inline-flex items-center cursor-pointer gap-2">
                            <input type="checkbox" id="input-reminder-enabled" class="w-4 h-4 accent-amber-500 rounded">
                            <span class="text-xs font-semibold text-slate-200">Enable Auto-Nudges</span>
                        </label>
                    </div>
                    <div class="grid md:grid-cols-3 gap-4">
                        <div class="space-y-1">
                            <label class="block text-xs font-semibold text-slate-400">Check Interval (Minutes)</label>
                            <input type="number" min="15" step="15" id="input-reminder-interval" value="240" class="w-full bg-dark-800 border border-slate-700 rounded-xl px-4 py-2 text-sm text-white focus:outline-none focus:border-amber-500 font-mono">
                        </div>
                        <div class="space-y-1">
                            <label class="block text-xs font-semibold text-slate-400">Active Start Hour (0-23 IST)</label>
                            <input type="number" min="0" max="23" id="input-reminder-start" value="8" class="w-full bg-dark-800 border border-slate-700 rounded-xl px-4 py-2 text-sm text-white focus:outline-none focus:border-amber-500 font-mono">
                        </div>
                        <div class="space-y-1">
                            <label class="block text-xs font-semibold text-slate-400">Active End Hour (0-23 IST)</label>
                            <input type="number" min="0" max="23" id="input-reminder-end" value="22" class="w-full bg-dark-800 border border-slate-700 rounded-xl px-4 py-2 text-sm text-white focus:outline-none focus:border-amber-500 font-mono">
                        </div>
                    </div>
                </div>

                <!-- Save Button Bar -->
                <div class="flex items-center justify-between p-4 bg-dark-800/80 rounded-2xl border border-slate-800">
                    <span class="text-xs text-slate-400">Settings save directly into your local <code class="text-sky-400 font-mono">.env</code> file.</span>
                    <button onclick="saveAllSettings()" class="bg-sky-600 hover:bg-sky-500 text-white font-bold px-6 py-2.5 rounded-xl text-sm transition shadow-lg shadow-sky-600/20 flex items-center gap-2">
                        <i data-lucide="save" class="w-4 h-4"></i> Save Settings
                    </button>
                </div>
            </div>

            <!-- ============================================================ -->
            <!-- TAB 2: GOOGLE TASKS HUB                                      -->
            <!-- ============================================================ -->
            <div id="tab-tasks" class="space-y-6 hidden">
                <div class="flex items-center justify-between">
                    <div>
                        <h2 class="text-xl font-bold text-white flex items-center gap-2">
                            <i data-lucide="check-square" class="w-5 h-5 text-emerald-400"></i> Google Tasks Sync
                        </h2>
                        <p class="text-sm text-slate-400">Real-time view of your active Google Tasks. Completing tasks here strikes them off in your Google Tasks app.</p>
                    </div>
                    <button onclick="loadTasks()" class="px-3 py-2 bg-slate-800 hover:bg-slate-700 text-slate-300 rounded-xl border border-slate-700 text-xs flex items-center gap-1.5 transition">
                        <i data-lucide="refresh-cw" class="w-3.5 h-3.5"></i> Refresh
                    </button>
                </div>

                <!-- Create New Task Form -->
                <div class="glass-panel p-4 rounded-2xl flex gap-3">
                    <input type="text" id="new-task-title" placeholder="Add a new streak task (e.g., 'AWS Community Post Check-in')..." class="flex-1 bg-dark-800 border border-slate-700 rounded-xl px-4 py-2.5 text-sm text-white focus:outline-none focus:border-emerald-500">
                    <button onclick="addNewTask()" class="bg-emerald-600 hover:bg-emerald-500 text-white font-semibold px-5 py-2.5 rounded-xl text-xs flex items-center gap-1.5 transition shadow-lg shadow-emerald-600/20">
                        <i data-lucide="plus" class="w-4 h-4"></i> Add Task
                    </button>
                </div>

                <!-- Tasks List -->
                <div id="tasks-container" class="space-y-3">
                    <div class="text-center py-12 text-slate-500">Loading tasks...</div>
                </div>
            </div>

            <!-- ============================================================ -->
            <!-- TAB 3: AI CHAT & WHATSAPP SIMULATOR                          -->
            <!-- ============================================================ -->
            <div id="tab-chat" class="space-y-6 hidden">
                <div>
                    <h2 class="text-xl font-bold text-white flex items-center gap-2">
                        <i data-lucide="message-square" class="w-5 h-5 text-sky-400"></i> AI Assistant & WhatsApp Simulator
                    </h2>
                    <p class="text-sm text-slate-400">Test how HabitSync AI responds with your real tasks. Try texting <span class="text-emerald-400 font-mono">"done"</span> to test task completion!</p>
                </div>

                <div class="glass-panel rounded-2xl flex flex-col h-[520px] overflow-hidden">
                    <!-- Chat Messages Log -->
                    <div id="chat-messages" class="flex-1 p-6 overflow-y-auto space-y-4">
                        <div class="flex items-start gap-3">
                            <div class="w-8 h-8 rounded-full bg-sky-500/20 text-sky-400 flex items-center justify-center shrink-0">
                                <i data-lucide="bot" class="w-4 h-4"></i>
                            </div>
                            <div class="bg-slate-800 border border-slate-700 rounded-2xl rounded-tl-none p-3.5 text-sm text-slate-200 max-w-lg leading-relaxed">
                                👋 Hey! I'm your HabitSync accountability buddy. I have live access to your Google Tasks. Ask me anything, or type <b>"done"</b> when you complete a task to check it off!
                            </div>
                        </div>
                    </div>

                    <!-- Quick Suggestions -->
                    <div class="px-6 py-2 bg-dark-800/50 border-t border-slate-800/80 flex gap-2 overflow-x-auto text-xs">
                        <button onclick="sendQuickMessage('What tasks do I have left today?')" class="px-3 py-1.5 rounded-full bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 shrink-0">
                            📌 What are my tasks?
                        </button>
                        <button onclick="sendQuickMessage('I feel lazy and procrastinating, motivate me!')" class="px-3 py-1.5 rounded-full bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 shrink-0">
                            ⚡ I feel lazy
                        </button>
                        <button onclick="sendQuickMessage('done')" class="px-3 py-1.5 rounded-full bg-emerald-500/10 hover:bg-emerald-500/20 text-emerald-300 border border-emerald-500/30 shrink-0 font-semibold">
                            ✅ done
                        </button>
                    </div>

                    <!-- Input Bar -->
                    <div class="p-4 bg-dark-800 border-t border-slate-800 flex gap-3">
                        <input type="text" id="chat-input" onkeydown="if(event.key==='Enter') sendChatMessage()" placeholder="Type a message (e.g., 'done' or 'help me plan my day')..." class="flex-1 bg-dark-900 border border-slate-700 rounded-xl px-4 py-2.5 text-sm text-white focus:outline-none focus:border-sky-500">
                        <button onclick="sendChatMessage()" class="bg-sky-600 hover:bg-sky-500 text-white font-semibold px-5 py-2.5 rounded-xl text-xs flex items-center gap-1.5 transition shadow-lg shadow-sky-600/20">
                            <i data-lucide="send" class="w-4 h-4"></i> Send
                        </button>
                    </div>
                </div>
            </div>

        </section>
    </main>

    <!-- Master Setup Guide Modal -->
    <div id="guide-modal" class="fixed inset-0 bg-black/80 backdrop-blur-sm z-50 flex items-center justify-center p-4 hidden">
        <div class="glass-panel bg-dark-800 border border-slate-700 max-w-2xl w-full rounded-2xl max-h-[90vh] flex flex-col overflow-hidden shadow-2xl">
            <div class="p-6 border-b border-slate-800 flex items-center justify-between">
                <div class="flex items-center gap-2.5">
                    <div class="p-2 rounded-lg bg-sky-500/10 text-sky-400">
                        <i data-lucide="book-marked" class="w-5 h-5"></i>
                    </div>
                    <div>
                        <h3 class="font-bold text-base text-white">HabitSync AI - Complete Setup Guide</h3>
                        <p class="text-xs text-slate-400">Everything you need to fill in the GUI (100% Free)</p>
                    </div>
                </div>
                <button onclick="closeHelpModal()" class="text-slate-400 hover:text-white p-2">
                    <i data-lucide="x" class="w-5 h-5"></i>
                </button>
            </div>

            <div class="p-6 overflow-y-auto space-y-6 text-sm text-slate-300">
                <!-- Part 1: Gemini -->
                <div class="space-y-2">
                    <div class="flex items-center gap-2 font-bold text-white text-sm">
                        <span class="w-6 h-6 rounded-lg bg-purple-500/20 text-purple-400 flex items-center justify-center text-xs">1</span>
                        <span>Google Gemini API Key (1 Minute)</span>
                    </div>
                    <p class="text-xs text-slate-400">Google Gemini provides the AI brain for conversing and coaching.</p>
                    <ol class="list-decimal ml-6 space-y-1 text-xs text-slate-300">
                        <li>Visit <a href="https://aistudio.google.com" target="_blank" class="text-sky-400 underline">aistudio.google.com</a>.</li>
                        <li>Click <b>"Get API key"</b> &rarr; <b>"Create API key"</b>.</li>
                        <li>Copy the key (starts with <code class="text-sky-300 font-mono">AIzaSy...</code>) and paste it into the <b>Gemini API Key</b> box.</li>
                    </ol>
                </div>

                <hr class="border-slate-800">

                <!-- Part 2: Google Tasks -->
                <div class="space-y-2">
                    <div class="flex items-center gap-2 font-bold text-white text-sm">
                        <span class="w-6 h-6 rounded-lg bg-blue-500/20 text-blue-400 flex items-center justify-center text-xs">2</span>
                        <span>Google Tasks credentials.json (3 Minutes)</span>
                    </div>
                    <p class="text-xs text-slate-400">Allows HabitSync to view and check off your real Google Tasks.</p>
                    <ol class="list-decimal ml-6 space-y-1.5 text-xs text-slate-300">
                        <li>Go to <a href="https://console.cloud.google.com" target="_blank" class="text-sky-400 underline">console.cloud.google.com</a> & create/select a project.</li>
                        <li>Search for <b>"Google Tasks API"</b> in the library and click <b>Enable</b>.</li>
                        <li>Go to <b>OAuth consent screen</b> &rarr; Choose <b>External</b> &rarr; Fill App name and email &rarr; Under <b>Test users</b>, add your own Gmail address &rarr; Save.</li>
                        <li>Go to <b>Credentials</b> &rarr; <b>Create Credentials</b> &rarr; <b>OAuth client ID</b> &rarr; Application Type: <b>Desktop app</b> &rarr; Leave 'AI-powered agent' UNCHECKED &rarr; Click Create.</li>
                        <li>Download the JSON file, then click <b>"Upload credentials.json"</b> in this dashboard.</li>
                        <li>Click <b>"Connect Google Account"</b> to finish login!</li>
                    </ol>
                </div>

                <hr class="border-slate-800">

                <!-- Part 3: Twilio WhatsApp -->
                <div class="space-y-2">
                    <div class="flex items-center gap-2 font-bold text-white text-sm">
                        <span class="w-6 h-6 rounded-lg bg-emerald-500/20 text-emerald-400 flex items-center justify-center text-xs">3</span>
                        <span>Twilio WhatsApp Sandbox (Free 2 Minutes)</span>
                    </div>
                    <p class="text-xs text-slate-400">Sends reminder messages to your phone and lets you text "done".</p>
                    <ol class="list-decimal ml-6 space-y-1.5 text-xs text-slate-300">
                        <li>Sign up at <a href="https://console.twilio.com" target="_blank" class="text-sky-400 underline">console.twilio.com</a> (Free tier gives $15 credit).</li>
                        <li>Copy <b>Account SID</b> & <b>Auth Token</b> from the dashboard into this GUI.</li>
                        <li>In Twilio sidebar: <b>Messaging &rarr; Try it out &rarr; Send a WhatsApp message</b>.</li>
                        <li>Send the join code (e.g. <code class="text-emerald-400 font-mono">join something</code>) from your phone's WhatsApp to <code class="text-sky-300 font-mono">+1 415 523 8886</code>.</li>
                        <li>Enter your number below: <code class="text-emerald-400 font-mono">whatsapp:+91XXXXXXXXXX</code> (include country code).</li>
                    </ol>
                </div>
            </div>

            <div class="p-4 bg-dark-900 border-t border-slate-800 flex justify-end">
                <button onclick="closeHelpModal()" class="px-5 py-2 bg-sky-600 hover:bg-sky-500 text-white rounded-xl text-xs font-semibold">
                    Got it, Let's Fill In!
                </button>
            </div>
        </div>
    </div>

    <!-- Toast Notification Container -->
    <div id="toast-container" class="fixed bottom-6 right-6 space-y-2 z-50"></div>

    <script>
        // Init Lucide Icons
        lucide.createIcons();

        // Guide toggles
        function toggleGuide(id) {
            const el = document.getElementById(id);
            el.classList.toggle('hidden');
            lucide.createIcons();
        }

        function openHelpModal() {
            document.getElementById('guide-modal').classList.remove('hidden');
            lucide.createIcons();
        }

        function closeHelpModal() {
            document.getElementById('guide-modal').classList.add('hidden');
        }

        // Switch Tabs
        function switchTab(tabId) {
            ['settings', 'tasks', 'chat'].forEach(t => {
                document.getElementById(`tab-${t}`).classList.add('hidden');
                document.getElementById(`tab-btn-${t}`).classList.remove('active');
            });
            document.getElementById(`tab-${tabId}`).classList.remove('hidden');
            document.getElementById(`tab-btn-${tabId}`).classList.add('active');

            if (tabId === 'tasks') loadTasks();
        }

        // Show Toast Helper
        function showToast(message, type = 'info') {
            const container = document.getElementById('toast-container');
            const toast = document.createElement('div');
            const bg = type === 'success' ? 'bg-emerald-600 border-emerald-500' :
                       type === 'error' ? 'bg-rose-600 border-rose-500' : 'bg-sky-600 border-sky-500';
            toast.className = `${bg} text-white px-4 py-3 rounded-xl border shadow-xl text-xs font-semibold flex items-center gap-2 transform transition-all duration-300 translate-y-2`;
            toast.innerHTML = `<span>${message}</span>`;
            container.appendChild(toast);
            setTimeout(() => toast.remove(), 4000);
        }

        function toggleVisibility(inputId) {
            const el = document.getElementById(inputId);
            el.type = el.type === 'password' ? 'text' : 'password';
        }

        // Fetch Status & Populate Inputs
        async function fetchStatus() {
            try {
                const res = await fetch('/api/status');
                const data = await res.json();
                
                // Badges
                updateBadge('badge-google', data.is_authenticated_google, 'Google Tasks');
                updateBadge('badge-gemini', data.status.gemini, 'Gemini AI');
                updateBadge('badge-telegram', data.status.telegram, 'Telegram');
                updateBadge('badge-twilio', data.status.twilio, 'WhatsApp');

                // Telegram Bot Card status
                const tgBadge = document.getElementById('telegram-bot-badge');
                if (tgBadge) {
                    if (data.status.telegram && data.credentials.telegram_is_polling) {
                        tgBadge.innerHTML = '<span class="w-2 h-2 rounded-full bg-emerald-400 animate-pulse"></span> <span class="text-emerald-400 font-semibold">Bot Online (Listening for messages)</span>';
                    } else if (data.status.telegram) {
                        tgBadge.innerHTML = '<span class="w-2 h-2 rounded-full bg-amber-400"></span> <span class="text-amber-400">Token verified (Send /start in Telegram)</span>';
                    } else {
                        tgBadge.innerHTML = '<span class="w-2 h-2 rounded-full bg-slate-500"></span> Bot waiting for token';
                    }
                }

                // Channel Selection
                if (data.credentials.notification_channel) {
                    const radio = document.querySelector(`input[name="notification_channel"][value="${data.credentials.notification_channel}"]`);
                    if (radio) radio.checked = true;
                    updateChannelBadge(data.credentials.notification_channel);
                }

                // Google Tasks Box
                const gPill = document.getElementById('google-status-pill');
                if (data.is_authenticated_google) {
                    gPill.className = 'text-xs px-3 py-1 rounded-full font-medium bg-emerald-500/20 text-emerald-400 border border-emerald-500/30';
                    gPill.textContent = 'Connected & Active';
                    document.getElementById('auth-token-status').textContent = '✅ Authorized';
                    document.getElementById('btn-google-auth').textContent = 'Re-Authenticate Google';
                } else if (data.credentials.has_credentials_json) {
                    gPill.className = 'text-xs px-3 py-1 rounded-full font-medium bg-amber-500/20 text-amber-400 border border-amber-500/30';
                    gPill.textContent = 'Needs Authorization';
                    document.getElementById('auth-token-status').textContent = 'Click button below';
                } else {
                    gPill.className = 'text-xs px-3 py-1 rounded-full font-medium bg-rose-500/20 text-rose-400 border border-rose-500/30';
                    gPill.textContent = 'Missing credentials.json';
                }

                document.getElementById('cred-file-status').textContent = data.credentials.has_credentials_json ? '✅ Present' : '❌ Missing';

                // Populate Inputs if empty
                if (!document.getElementById('input-gemini-key').value && data.credentials.gemini_api_key_masked) {
                    document.getElementById('input-gemini-key').placeholder = data.credentials.gemini_api_key_masked;
                }
                if (!document.getElementById('input-twilio-sid').value && data.credentials.twilio_account_sid_masked) {
                    document.getElementById('input-twilio-sid').placeholder = data.credentials.twilio_account_sid_masked;
                }
                if (!document.getElementById('input-twilio-number').value && data.credentials.twilio_whatsapp_number) {
                    document.getElementById('input-twilio-number').value = data.credentials.twilio_whatsapp_number;
                }
                if (data.credentials.twilio_content_sid) {
                    document.getElementById('input-twilio-content-sid').value = data.credentials.twilio_content_sid;
                }
                if (!document.getElementById('input-user-number').value && data.credentials.user_whatsapp_number) {
                    document.getElementById('input-user-number').value = data.credentials.user_whatsapp_number;
                }
                if (!document.getElementById('input-telegram-token').value && data.credentials.telegram_bot_token_masked) {
                    document.getElementById('input-telegram-token').placeholder = data.credentials.telegram_bot_token_masked;
                }
                if (!document.getElementById('input-telegram-chat-id').value && data.credentials.telegram_chat_id) {
                    document.getElementById('input-telegram-chat-id').value = data.credentials.telegram_chat_id;
                }
                document.getElementById('input-reminder-enabled').checked = Boolean(data.credentials.reminder_enabled);
                if (data.credentials.reminder_interval_minutes) {
                    document.getElementById('input-reminder-interval').value = data.credentials.reminder_interval_minutes;
                }
                if (data.credentials.reminder_start_hour !== undefined) {
                    document.getElementById('input-reminder-start').value = data.credentials.reminder_start_hour;
                }
                if (data.credentials.reminder_end_hour !== undefined) {
                    document.getElementById('input-reminder-end').value = data.credentials.reminder_end_hour;
                }
            } catch (e) {
                console.error(e);
            }
        }

        function updateChannelBadge(channel) {
            const pill = document.getElementById('active-channel-pill');
            if (!pill) return;
            if (channel === 'telegram') {
                pill.className = 'text-xs px-3 py-1 rounded-full font-bold bg-sky-500/20 text-sky-400 border border-sky-500/30';
                pill.textContent = '✈️ Telegram Active';
            } else if (channel === 'whatsapp') {
                pill.className = 'text-xs px-3 py-1 rounded-full font-bold bg-emerald-500/20 text-emerald-400 border border-emerald-500/30';
                pill.textContent = '💬 WhatsApp Active';
            } else {
                pill.className = 'text-xs px-3 py-1 rounded-full font-bold bg-purple-500/20 text-purple-400 border border-purple-500/30';
                pill.textContent = '⚡ Both Active';
            }
        }

        function updateBadge(badgeId, isOk, label) {
            const el = document.getElementById(badgeId);
            if (!el) return;
            const dot = el.querySelector('span');
            if (isOk) {
                el.className = 'px-2.5 py-1 rounded-full bg-emerald-500/10 border border-emerald-500/30 flex items-center gap-1.5 text-emerald-400 font-medium';
                dot.className = 'w-2 h-2 rounded-full bg-emerald-400 animate-pulse';
            } else {
                el.className = 'px-2.5 py-1 rounded-full bg-slate-800 border border-slate-700 flex items-center gap-1.5 text-slate-400';
                dot.className = 'w-2 h-2 rounded-full bg-slate-500';
            }
        }

        // Save All Settings
        async function saveAllSettings() {
            const payload = {};
            const gKey = document.getElementById('input-gemini-key').value.trim();
            if (gKey) payload.gemini_api_key = gKey;

            const tSid = document.getElementById('input-twilio-sid').value.trim();
            if (tSid) payload.twilio_account_sid = tSid;

            const tTok = document.getElementById('input-twilio-token').value.trim();
            if (tTok) payload.twilio_auth_token = tTok;

            const tNum = document.getElementById('input-twilio-number').value.trim();
            if (tNum) payload.twilio_whatsapp_number = tNum;

            const tContentSid = document.getElementById('input-twilio-content-sid').value.trim();
            if (tContentSid) payload.twilio_content_sid = tContentSid;

            const uNum = document.getElementById('input-user-number').value.trim();
            if (uNum) payload.user_whatsapp_number = uNum;

            const tgToken = document.getElementById('input-telegram-token').value.trim();
            if (tgToken) payload.telegram_bot_token = tgToken;

            const tgChatId = document.getElementById('input-telegram-chat-id').value.trim();
            if (tgChatId) payload.telegram_chat_id = tgChatId;

            const selectedChannel = document.querySelector('input[name="notification_channel"]:checked');
            if (selectedChannel) payload.notification_channel = selectedChannel.value;

            payload.reminder_enabled = document.getElementById('input-reminder-enabled').checked;
            const rInt = parseInt(document.getElementById('input-reminder-interval').value, 10);
            if (!isNaN(rInt)) payload.reminder_interval_minutes = rInt;
            const rStart = parseInt(document.getElementById('input-reminder-start').value, 10);
            if (!isNaN(rStart)) payload.reminder_start_hour = rStart;
            const rEnd = parseInt(document.getElementById('input-reminder-end').value, 10);
            if (!isNaN(rEnd)) payload.reminder_end_hour = rEnd;

            try {
                const res = await fetch('/api/settings', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(payload)
                });
                const data = await res.json();
                showToast(data.message, 'success');
                fetchStatus();
            } catch (e) {
                showToast('Failed to save settings: ' + e, 'error');
            }
        }

        // Upload Credentials File
        async function uploadCredentialsFile(event) {
            const file = event.target.files[0];
            if (!file) return;

            const formData = new FormData();
            formData.append('file', file);

            try {
                const res = await fetch('/api/upload-credentials', {
                    method: 'POST',
                    body: formData
                });
                const data = await res.json();
                if (res.ok) {
                    showToast('credentials.json uploaded successfully!', 'success');
                    fetchStatus();
                } else {
                    showToast(data.detail || 'Upload failed', 'error');
                }
            } catch (e) {
                showToast('Upload error: ' + e, 'error');
            }
        }

        // Google Auth Trigger
        async function startGoogleAuth() {
            showToast('Opening Google Sign-in in your browser...', 'info');
            try {
                const res = await fetch('/api/auth/google', {method: 'POST'});
                const data = await res.json();
                showToast(data.message, 'info');

                // Poll every 3 seconds for completion
                const interval = setInterval(async () => {
                    const statusRes = await fetch('/api/status');
                    const statusData = await statusRes.json();
                    if (statusData.is_authenticated_google) {
                        clearInterval(interval);
                        showToast('🎉 Google Tasks Connected Successfully!', 'success');
                        fetchStatus();
                    }
                }, 3000);
            } catch (e) {
                showToast('Auth error: ' + e, 'error');
            }
        }

        // Test Gemini (Auto-passes whatever is in the input box)
        async function testGeminiKey() {
            const key = document.getElementById('input-gemini-key').value.trim();
            showToast('Testing Google Gemini connection...', 'info');
            try {
                const res = await fetch('/api/test/gemini', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({gemini_api_key: key || null})
                });
                const data = await res.json();
                if (res.ok) {
                    showToast('✅ Gemini Connected! Response: "' + data.response + '"', 'success');
                    fetchStatus();
                } else {
                    showToast(data.detail || 'Gemini check failed', 'error');
                }
            } catch (e) {
                showToast('Connection error: ' + e, 'error');
            }
        }

        // Test Twilio (Auto-passes whatever is in the input boxes)
        async function testTwilio() {
            const payload = {
                twilio_account_sid: document.getElementById('input-twilio-sid').value.trim() || null,
                twilio_auth_token: document.getElementById('input-twilio-token').value.trim() || null,
                twilio_whatsapp_number: document.getElementById('input-twilio-number').value.trim() || null,
                twilio_content_sid: document.getElementById('input-twilio-content-sid').value.trim() || null,
                user_whatsapp_number: document.getElementById('input-user-number').value.trim() || null
            };
            showToast('Sending test WhatsApp message...', 'info');
            try {
                const res = await fetch('/api/test/twilio', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(payload)
                });
                const data = await res.json();
                if (res.ok) {
                    showToast('✅ ' + data.message + ' (SID: ' + data.message_sid + ')', 'success');
                    fetchStatus();
                } else {
                    showToast(data.detail || 'Twilio test failed', 'error');
                }
            } catch (e) {
                showToast('Twilio error: ' + e, 'error');
            }
        }

        // Test Telegram Bot
        async function testTelegram() {
            const payload = {
                telegram_bot_token: document.getElementById('input-telegram-token').value.trim() || null,
                telegram_chat_id: document.getElementById('input-telegram-chat-id').value.trim() || null
            };
            showToast('Connecting to Telegram Bot API...', 'info');
            try {
                const res = await fetch('/api/test/telegram', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(payload)
                });
                const data = await res.json();
                if (res.ok) {
                    showToast('✅ ' + data.message, 'success');
                    fetchStatus();
                } else {
                    showToast(data.detail || 'Telegram verification failed', 'error');
                }
            } catch (e) {
                showToast('Telegram error: ' + e, 'error');
            }
        }

        // Load Google Tasks
        async function loadTasks() {
            const container = document.getElementById('tasks-container');
            container.innerHTML = '<div class="text-center py-12 text-slate-500">Loading tasks...</div>';
            try {
                const res = await fetch('/api/tasks');
                const data = await res.json();
                if (data.status === 'unauthenticated') {
                    container.innerHTML = `
                        <div class="glass-panel p-8 rounded-2xl text-center space-y-3">
                            <i data-lucide="lock" class="w-10 h-10 text-amber-400 mx-auto"></i>
                            <h3 class="font-semibold text-white">Google Tasks Not Connected</h3>
                            <p class="text-xs text-slate-400">Please authorize Google Tasks in the API & Setup tab first.</p>
                            <button onclick="switchTab('settings')" class="px-4 py-2 bg-blue-600 text-white rounded-xl text-xs font-semibold">Go to Setup</button>
                        </div>
                    `;
                    lucide.createIcons();
                    return;
                }

                const tasks = data.tasks || [];
                const pill = document.getElementById('tasks-count-pill');
                pill.textContent = tasks.length;
                pill.classList.remove('hidden');

                if (tasks.length === 0) {
                    container.innerHTML = `
                        <div class="glass-panel p-8 rounded-2xl text-center space-y-2">
                            <i data-lucide="party-popper" class="w-10 h-10 text-emerald-400 mx-auto"></i>
                            <h3 class="font-semibold text-white">All Caught Up!</h3>
                            <p class="text-xs text-slate-400">You have no pending tasks in Google Tasks. Your streak is safe! 🔥</p>
                        </div>
                    `;
                    lucide.createIcons();
                    return;
                }

                container.innerHTML = tasks.map((t, idx) => `
                    <div class="glass-panel p-4 rounded-xl flex items-center justify-between border border-slate-800 hover:border-slate-700 transition">
                        <div class="flex items-center gap-3">
                            <div class="w-7 h-7 rounded-lg bg-emerald-500/10 text-emerald-400 font-bold flex items-center justify-center text-xs">
                                ${idx + 1}
                            </div>
                            <div>
                                <h4 class="font-semibold text-sm text-white">${t.title}</h4>
                                <span class="text-xs text-slate-400 font-mono">${t.due ? 'Due: ' + t.due : 'No due date'}</span>
                            </div>
                        </div>
                        <button onclick="completeTask('${t.id}')" class="px-3.5 py-1.5 rounded-lg bg-emerald-500/10 hover:bg-emerald-500/20 text-emerald-300 border border-emerald-500/30 text-xs font-semibold flex items-center gap-1.5 transition">
                            <i data-lucide="check" class="w-3.5 h-3.5"></i> Mark Done
                        </button>
                    </div>
                `).join('');
                lucide.createIcons();
            } catch (e) {
                container.innerHTML = `<div class="p-4 bg-rose-500/10 text-rose-400 text-xs rounded-xl">Error loading tasks: ${e}</div>`;
            }
        }

        async function addNewTask() {
            const input = document.getElementById('new-task-title');
            const title = input.value.trim();
            if (!title) return;
            try {
                const res = await fetch('/api/tasks', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({title})
                });
                if (res.ok) {
                    input.value = '';
                    showToast('Task added to Google Tasks!', 'success');
                    loadTasks();
                } else {
                    const data = await res.json();
                    showToast(data.detail || 'Failed to add task', 'error');
                }
            } catch (e) {
                showToast('Error: ' + e, 'error');
            }
        }

        async function completeTask(taskId) {
            try {
                const res = await fetch(`/api/tasks/${taskId}/complete`, {method: 'POST'});
                if (res.ok) {
                    showToast('Task marked as completed! Streak safe! 🔥', 'success');
                    loadTasks();
                } else {
                    showToast('Failed to complete task', 'error');
                }
            } catch (e) {
                showToast('Error: ' + e, 'error');
            }
        }

        // Chat & WhatsApp Simulator
        async function sendChatMessage() {
            const input = document.getElementById('chat-input');
            const msg = input.value.trim();
            if (!msg) return;

            input.value = '';
            appendMessage('user', msg);

            try {
                const res = await fetch('/api/chat', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({message: msg})
                });
                const data = await res.json();
                appendMessage('bot', data.response);
                if (data.action === 'completed') {
                    showToast('Task auto-completed via chat!', 'success');
                    if (!document.getElementById('tab-tasks').classList.contains('hidden')) {
                        loadTasks();
                    }
                }
            } catch (e) {
                appendMessage('bot', '⚠️ Error contacting assistant: ' + e);
            }
        }

        function sendQuickMessage(text) {
            document.getElementById('chat-input').value = text;
            sendChatMessage();
        }

        function appendMessage(sender, text) {
            const container = document.getElementById('chat-messages');
            const wrapper = document.createElement('div');
            wrapper.className = sender === 'user' ? 'flex items-start justify-end gap-3' : 'flex items-start gap-3';

            const formattedText = text.replace(/\\*(.*?)\\*/g, '<b>$1</b>').replace(/\\n/g, '<br>');

            if (sender === 'user') {
                wrapper.innerHTML = `
                    <div class="bg-sky-600 rounded-2xl rounded-tr-none p-3.5 text-sm text-white max-w-lg leading-relaxed shadow-md">
                        ${formattedText}
                    </div>
                    <div class="w-8 h-8 rounded-full bg-slate-700 text-slate-300 flex items-center justify-center shrink-0">
                        <i data-lucide="user" class="w-4 h-4"></i>
                    </div>
                `;
            } else {
                wrapper.innerHTML = `
                    <div class="w-8 h-8 rounded-full bg-sky-500/20 text-sky-400 flex items-center justify-center shrink-0">
                        <i data-lucide="bot" class="w-4 h-4"></i>
                    </div>
                    <div class="bg-slate-800 border border-slate-700 rounded-2xl rounded-tl-none p-3.5 text-sm text-slate-200 max-w-lg leading-relaxed">
                        ${formattedText}
                    </div>
                `;
            }
            container.appendChild(wrapper);
            container.scrollTop = container.scrollHeight;
            lucide.createIcons();
        }

        // Trigger Habit Nudge Across Active Channel(s)
        async function triggerReminderNow() {
            showToast('Sending task reminder across active channel(s)...', 'info');
            try {
                const res = await fetch('/api/send-reminder', {method: 'POST'});
                const data = await res.json();
                if (res.ok) {
                    const sentChannels = Object.keys(data.dispatches || {})
                        .filter(k => data.dispatches[k].status === 'sent')
                        .map(k => k === 'telegram' ? '✈️ Telegram' : '💬 WhatsApp')
                        .join(' & ');

                    if (sentChannels) {
                        showToast(`🎉 Nudge dispatched via ${sentChannels}! (${data.tasks_count} tasks)`, 'success');
                    } else {
                        const details = Object.entries(data.dispatches || {})
                            .map(([k, v]) => `${k}: ${v.error || v.reason || v.status}`)
                            .join(' | ');
                        showToast(`No channel delivered (${details || data.channel})`, 'error');
                    }
                } else {
                    showToast(data.detail || 'Failed to send reminder', 'error');
                }
            } catch (e) {
                showToast('Reminder error: ' + e, 'error');
            }
        }

        // Initial Load
        fetchStatus();
    </script>
</body>
</html>
"""


if __name__ == "__main__":
    import uvicorn
    enable_reload = os.getenv("LOCAL_RELOAD", "true").lower() == "true" and not config.IS_CLOUD
    uvicorn.run("main:app", host=config.HOST, port=config.PORT, reload=enable_reload)
