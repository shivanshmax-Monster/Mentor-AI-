import html
import logging
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from config import config
from database import User, get_db, init_db
from gemini_service import gemini_service
from google_tasks_service import WEB_OAUTH_SCOPES, get_web_oauth_client_config
from security import (
    OAUTH_STATE_COOKIE_NAME,
    SAAS_COOKIE_NAME,
    create_oauth_state_token,
    create_user_session_token,
    decrypt_secret,
    encrypt_secret,
    generate_link_token,
    mask_secret,
    normalize_whatsapp_number,
    verify_oauth_state_token,
    verify_user_session_token,
)
from telegram_service import telegram_service
from tenant_router import (
    DEFAULT_LIST_KEYWORDS,
    process_user_chat_result,
    resolve_tasks_client_for_user,
)
from whatsapp_service import whatsapp_service

logger = logging.getLogger("SaaSRoutes")

router = APIRouter()

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


class ValidateGeminiPayload(BaseModel):
    api_key: str


class OnboardingPayload(BaseModel):
    whatsapp_phone: Optional[str] = None
    gemini_api_key: Optional[str] = None
    notification_channel: str = "telegram"
    reminder_enabled: bool = True
    reminder_interval_minutes: int = 240
    reminder_timezone: str = "Asia/Kolkata"
    reminder_start_hour: int = 8
    reminder_end_hour: int = 22


class SaasCreateTaskPayload(BaseModel):
    title: str
    notes: Optional[str] = None
    due: Optional[str] = None


class SaasChatPayload(BaseModel):
    message: str


def _is_https_request(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return proto == "https" or config.IS_CLOUD


def _resolve_callback_url(request: Request) -> str:
    """Computes the exact authorized Google OAuth callback URL."""
    if config.PUBLIC_BASE_URL:
        return f"{config.PUBLIC_BASE_URL.rstrip('/')}/auth/google/callback"
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}/auth/google/callback"


def _exchange_google_code(code: str, redirect_uri: str) -> Dict[str, Any]:
    """Exchanges an authorization code for OAuth tokens from Google."""
    web_cfg = get_web_oauth_client_config()
    resp = httpx.post(
        GOOGLE_TOKEN_URL,
        data={
            "code": code,
            "client_id": web_cfg["client_id"],
            "client_secret": web_cfg["client_secret"],
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=20.0,
    )
    if resp.status_code != 200:
        raise HTTPException(status_code=400, detail=f"Google token exchange failed: {resp.text}")
    return resp.json()


def _fetch_google_userinfo(access_token: str) -> Dict[str, Any]:
    """Fetches authenticated Google account profile info."""
    resp = httpx.get(
        GOOGLE_USERINFO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15.0,
    )
    if resp.status_code != 200:
        raise HTTPException(status_code=400, detail=f"Failed to fetch Google user profile: {resp.text}")
    return resp.json()


def get_optional_current_user(request: Request, db: Session = Depends(get_db)) -> Optional[User]:
    """Resolves the logged-in SaaS user from their signed session cookie, if present."""
    token = request.cookies.get(SAAS_COOKIE_NAME)
    user_id = verify_user_session_token(token)
    if not user_id:
        return None
    return db.query(User).filter(User.id == user_id).first()


def get_current_user(user: Optional[User] = Depends(get_optional_current_user)) -> User:
    """Requires an authenticated SaaS user session."""
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required. Please sign in with Google.")
    return user


def _build_me_payload(user: User) -> Dict[str, Any]:
    """Builds safe SaaS profile payload including Telegram deep link & setup checklist."""
    masked = ""
    if user.gemini_api_key_enc:
        try:
            masked = mask_secret(decrypt_secret(user.gemini_api_key_enc))
        except ValueError:
            masked = "****"

    if not user.telegram_link_token:
        user.telegram_link_token = generate_link_token()

    data = user.to_public_dict(masked_gemini=masked)
    bot_username = config.TELEGRAM_BOT_USERNAME or "HabitSyncShivanshBot"
    data["telegram_bot_username"] = bot_username
    data["telegram_deep_link"] = f"https://t.me/{bot_username}?start=link_{user.telegram_link_token}"
    data["google_web_oauth_ready"] = config.has_google_web_oauth()
    data["setup_progress"] = {
        "google_connected": bool(user.google_refresh_token_enc),
        "gemini_verified": bool(user.gemini_api_key_enc and user.gemini_key_verified),
        "channel_connected": bool(user.telegram_chat_id or user.whatsapp_number),
    }
    return data


@router.get("/auth/google/login")
def google_oauth_login(request: Request):
    """Starts the multi-tenant Google Web OAuth 2.0 flow."""
    if not config.has_google_web_oauth():
        redirect_uri = _resolve_callback_url(request)
        raise HTTPException(
            status_code=503,
            detail=(
                "Google Web OAuth is not configured yet. Set GOOGLE_WEB_CLIENT_ID and "
                f"GOOGLE_WEB_CLIENT_SECRET with Authorized Redirect URI: {redirect_uri}"
            ),
        )

    web_cfg = get_web_oauth_client_config()
    redirect_uri = _resolve_callback_url(request)
    state = create_oauth_state_token()

    params = {
        "client_id": web_cfg["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(WEB_OAUTH_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    auth_url = f"{GOOGLE_AUTH_URL}?{urlencode(params)}"
    resp = RedirectResponse(url=auth_url, status_code=302)
    resp.set_cookie(
        key=OAUTH_STATE_COOKIE_NAME,
        value=state,
        httponly=True,
        samesite="lax",
        secure=_is_https_request(request),
        max_age=600,
    )
    return resp


@router.get("/auth/google/callback")
def google_oauth_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Handles Google OAuth 2.0 callback, encrypts refresh token, and issues user cookie."""
    if error:
        return RedirectResponse(url=f"/saas?oauth_error={error}", status_code=302)

    cookie_state = request.cookies.get(OAUTH_STATE_COOKIE_NAME, "")
    if not code or not state or not cookie_state or state != cookie_state or not verify_oauth_state_token(state):
        raise HTTPException(status_code=400, detail="Invalid or expired Google OAuth state. Please try again.")

    redirect_uri = _resolve_callback_url(request)
    token_data = _exchange_google_code(code=code, redirect_uri=redirect_uri)
    access_token = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token")

    if not access_token:
        raise HTTPException(status_code=400, detail="Google did not return an access token.")

    profile = _fetch_google_userinfo(access_token)
    email = (profile.get("email") or "").strip().lower()
    google_sub = profile.get("sub")
    if not email:
        raise HTTPException(status_code=400, detail="Google account did not provide an email address.")

    init_db()
    user = None
    if google_sub:
        user = db.query(User).filter(User.google_sub == google_sub).first()
    if user is None:
        user = db.query(User).filter(User.email == email).first()

    if user is None:
        if not refresh_token:
            raise HTTPException(
                status_code=400,
                detail="Google did not grant offline refresh access. Please remove HabitSync from Google permissions and retry.",
            )
        user = User(
            email=email,
            google_sub=google_sub,
            full_name=profile.get("name"),
            avatar_url=profile.get("picture"),
            google_refresh_token_enc=encrypt_secret(refresh_token),
            telegram_link_token=generate_link_token(),
        )
        db.add(user)
    else:
        user.google_sub = google_sub or user.google_sub
        user.full_name = profile.get("name") or user.full_name
        user.avatar_url = profile.get("picture") or user.avatar_url
        if refresh_token:
            user.google_refresh_token_enc = encrypt_secret(refresh_token)
        if not user.telegram_link_token:
            user.telegram_link_token = generate_link_token()

    db.commit()
    db.refresh(user)

    resp = RedirectResponse(url="/saas", status_code=303)
    resp.delete_cookie(OAUTH_STATE_COOKIE_NAME)
    resp.set_cookie(
        key=SAAS_COOKIE_NAME,
        value=create_user_session_token(user.id),
        httponly=True,
        samesite="lax",
        secure=_is_https_request(request),
        max_age=60 * 60 * 24 * 30,
    )
    return resp


@router.get("/auth/logout")
@router.post("/auth/logout")
def saas_logout(request: Request):
    """Signs out the current SaaS user by clearing their session cookie."""
    if request.method == "GET":
        resp = RedirectResponse(url="/saas", status_code=302)
    else:
        resp = JSONResponse({"ok": True})
    resp.delete_cookie(SAAS_COOKIE_NAME)
    return resp


@router.get("/api/saas/me")
def get_saas_me(user: Optional[User] = Depends(get_optional_current_user), db: Session = Depends(get_db)):
    """Returns authentication state and profile for the SaaS portal."""
    if not user:
        return {
            "authenticated": False,
            "google_web_oauth_ready": config.has_google_web_oauth(),
            "telegram_bot_username": config.TELEGRAM_BOT_USERNAME or "HabitSyncShivanshBot",
        }
    payload = _build_me_payload(user)
    db.commit()
    return {"authenticated": True, "user": payload}


@router.post("/api/saas/validate-gemini")
def validate_saas_gemini(payload: ValidateGeminiPayload):
    """Tests a BYOK Gemini API key in real time against Google Gemini."""
    is_valid, message = gemini_service.validate_api_key(payload.api_key)
    if not is_valid:
        raise HTTPException(status_code=400, detail=message)
    return {"valid": True, "message": message}


@router.post("/api/saas/onboarding")
def save_saas_onboarding(
    payload: OnboardingPayload,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Validates and saves encrypted BYOK Gemini key, E.164 WhatsApp phone, and schedule preferences."""
    channel = (payload.notification_channel or "telegram").strip().lower()
    if channel not in ("telegram", "whatsapp", "both"):
        raise HTTPException(status_code=422, detail="notification_channel must be 'telegram', 'whatsapp', or 'both'.")

    if not (15 <= int(payload.reminder_interval_minutes) <= 1440):
        raise HTTPException(status_code=422, detail="reminder_interval_minutes must be between 15 and 1440.")
    if not (0 <= int(payload.reminder_start_hour) <= 23 and 0 <= int(payload.reminder_end_hour) <= 23):
        raise HTTPException(status_code=422, detail="Active hours must be between 0 and 23.")

    raw_phone = (payload.whatsapp_phone or "").strip()
    if raw_phone:
        try:
            normalized_phone = normalize_whatsapp_number(raw_phone)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

        conflict = (
            db.query(User)
            .filter(User.whatsapp_number == normalized_phone, User.id != user.id)
            .first()
        )
        if conflict:
            raise HTTPException(status_code=409, detail="This WhatsApp number is already linked to another account.")
        user.whatsapp_number = normalized_phone
    else:
        if channel in ("whatsapp", "both") and not user.whatsapp_number:
            raise HTTPException(
                status_code=422,
                detail="Please enter a WhatsApp phone number with country code (e.g., +919876543210).",
            )
        if payload.whatsapp_phone is not None and raw_phone == "":
            user.whatsapp_number = None

    raw_gemini = (payload.gemini_api_key or "").strip()
    if raw_gemini and "..." not in raw_gemini and not raw_gemini.startswith("****"):
        is_valid, msg = gemini_service.validate_api_key(raw_gemini)
        if not is_valid:
            raise HTTPException(status_code=400, detail=msg)
        user.gemini_api_key_enc = encrypt_secret(raw_gemini)
        user.gemini_key_verified = True

    user.notification_channel = channel
    user.reminder_enabled = bool(payload.reminder_enabled)
    user.reminder_interval_minutes = int(payload.reminder_interval_minutes)
    user.reminder_timezone = (payload.reminder_timezone or "Asia/Kolkata").strip()
    user.reminder_start_hour = int(payload.reminder_start_hour)
    user.reminder_end_hour = int(payload.reminder_end_hour)

    db.commit()
    db.refresh(user)
    return {"status": "success", "user": _build_me_payload(user)}


@router.post("/api/saas/telegram/regenerate-link")
def regenerate_telegram_link(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Generates a fresh Telegram /start link_<token> invite URL for the signed-in user."""
    user.telegram_link_token = generate_link_token()
    db.commit()
    db.refresh(user)
    return {"user": _build_me_payload(user)}


@router.get("/api/saas/tasks")
def get_saas_tasks(user: User = Depends(get_current_user)):
    """Lists pending Google Tasks strictly for the signed-in SaaS user."""
    try:
        client = resolve_tasks_client_for_user(user)
        tasks = client.get_pending_tasks()
        return {"tasks": tasks, "count": len(tasks)}
    except Exception as exc:
        logger.error(f"SaaS tasks fetch failed for {user.email}: {exc}")
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/api/saas/tasks")
def create_saas_task(payload: SaasCreateTaskPayload, user: User = Depends(get_current_user)):
    """Creates a task in the signed-in SaaS user's Google Tasks list."""
    try:
        client = resolve_tasks_client_for_user(user)
        task = client.create_task(title=payload.title, notes=payload.notes, due=payload.due)
        return {"status": "success", "task": task}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/api/saas/tasks/{task_id}/complete")
def complete_saas_task(task_id: str, user: User = Depends(get_current_user)):
    """Marks a task completed strictly in the signed-in SaaS user's Google Tasks."""
    try:
        client = resolve_tasks_client_for_user(user)
        task = client.complete_task(task_id)
        return {"status": "success", "task": task}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/api/saas/chat")
def saas_chat(payload: SaasChatPayload, user: User = Depends(get_current_user)):
    """Runs chat & completion simulation strictly with this user's tasks & BYOK Gemini key."""
    return process_user_chat_result(
        user=user,
        user_text=payload.message,
        session_key=f"saas_{user.id}",
        list_keywords=DEFAULT_LIST_KEYWORDS,
        formatter_fn=whatsapp_service.format_reminder_text,
    )


@router.post("/api/saas/send-nudge")
def send_saas_nudge(user: User = Depends(get_current_user)):
    """Dispatches an immediate test accountability nudge for the signed-in user."""
    try:
        client = resolve_tasks_client_for_user(user)
        pending = client.get_pending_tasks()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Google Tasks error: {exc}")

    dispatches: Dict[str, Any] = {}
    channel = user.notification_channel

    if channel in ("telegram", "both"):
        if user.telegram_chat_id and config.TELEGRAM_BOT_TOKEN:
            try:
                res = telegram_service.send_task_reminder(pending_tasks=pending, chat_id=user.telegram_chat_id)
                dispatches["telegram"] = {"status": "sent", "msg_id": res.get("message_id")}
            except Exception as exc:
                dispatches["telegram"] = {"status": "error", "error": str(exc)}
        else:
            dispatches["telegram"] = {"status": "skipped", "reason": "Telegram is not linked yet"}

    if channel in ("whatsapp", "both"):
        if user.whatsapp_number and config.TWILIO_ACCOUNT_SID:
            try:
                sid = whatsapp_service.send_task_reminder(user.whatsapp_number, pending)
                dispatches["whatsapp"] = {"status": "sent", "sid": sid}
            except Exception as exc:
                dispatches["whatsapp"] = {"status": "error", "error": str(exc)}
        else:
            dispatches["whatsapp"] = {"status": "skipped", "reason": "WhatsApp number or Twilio missing"}

    return {"status": "success", "tasks_count": len(pending), "dispatches": dispatches}


@router.get("/saas", response_class=HTMLResponse)
def saas_portal(request: Request, oauth_error: Optional[str] = None):
    """Serves the Phase 2 Multi-Tenant SaaS Onboarding & User Workspace UI."""
    callback_url = _resolve_callback_url(request)
    oauth_err_banner = ""
    if oauth_error:
        safe_err = html.escape(oauth_error)
        oauth_err_banner = (
            f'<div class="mb-6 bg-rose-500/10 border border-rose-500/30 text-rose-300 '
            f'text-xs rounded-xl px-4 py-3">Google Sign-In returned an error: <code>{safe_err}</code></div>'
        )
    return HTMLResponse(content=render_saas_html(callback_url=callback_url, oauth_error_banner=oauth_err_banner))


def render_saas_html(callback_url: str, oauth_error_banner: str = "") -> str:
    """Renders the unified Tailwind multi-tenant SaaS web experience."""
    safe_cb = html.escape(callback_url)
    return f"""<!DOCTYPE html>
<html lang="en" class="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>HabitSync AI — Multi-User SaaS Portal</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script>
        tailwind.config = {{
            darkMode: 'class',
            theme: {{
                extend: {{
                    colors: {{
                        brand: {{ 500: '#38bdf8', 600: '#0284c7' }},
                        dark: {{ 900: '#0b0f19', 800: '#111827', 700: '#1f2937' }}
                    }}
                }}
            }}
        }}
    </script>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap');
        body {{ font-family: 'Plus Jakarta Sans', sans-serif; }}
        .glass-card {{ background: rgba(17, 24, 39, 0.88); backdrop-filter: blur(14px); border: 1px solid rgba(255,255,255,0.08); }}
    </style>
</head>
<body class="bg-dark-900 text-slate-100 min-h-screen flex flex-col">
    <header class="border-b border-slate-800/80 bg-dark-900/90 sticky top-0 z-20 backdrop-blur">
        <div class="max-w-6xl mx-auto px-4 sm:px-6 h-16 flex items-center justify-between gap-4">
            <div class="flex items-center gap-3">
                <div class="w-9 h-9 rounded-xl bg-gradient-to-br from-sky-400 to-indigo-600 flex items-center justify-center font-bold text-white shadow-lg shadow-sky-500/20">H</div>
                <div>
                    <div class="font-bold text-white leading-tight flex items-center gap-2">
                        HabitSync AI
                        <span class="text-[10px] uppercase tracking-wider px-2 py-0.5 rounded-full bg-sky-500/15 text-sky-400 border border-sky-500/30">SaaS Beta</span>
                    </div>
                    <p class="text-[11px] text-slate-400">Encrypted Multi-Tenant Habit Accountability</p>
                </div>
            </div>
            <div class="flex items-center gap-3">
                <a href="/" class="text-xs text-slate-400 hover:text-white transition hidden sm:inline">Owner Console</a>
                <div id="header-user" class="hidden items-center gap-3">
                    <span id="voice-badge" class="hidden md:inline-flex items-center gap-1.5 text-[11px] px-2.5 py-1 rounded-full bg-violet-500/10 text-violet-300 border border-violet-500/30">
                        📞 AI Voice: <strong id="voice-mins">0m</strong> (Part B Soon)
                    </span>
                    <div class="flex items-center gap-2 bg-slate-800/70 border border-slate-700 rounded-full pl-1.5 pr-3 py-1">
                        <img id="user-avatar" src="" alt="" class="w-6 h-6 rounded-full bg-slate-700 object-cover hidden">
                        <span id="user-email" class="text-xs text-slate-200 max-w-[160px] truncate"></span>
                    </div>
                    <button onclick="logout()" class="text-xs px-3 py-1.5 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-300 transition">Sign Out</button>
                </div>
            </div>
        </div>
    </header>

    <main class="max-w-6xl w-full mx-auto px-4 sm:px-6 py-8 flex-1">
        {oauth_error_banner}

        <!-- VIEW 1: UNAUTHENTICATED LANDING -->
        <section id="landing-view" class="py-8 sm:py-14 space-y-10">
            <div class="max-w-3xl mx-auto text-center space-y-5">
                <div class="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-emerald-500/10 border border-emerald-500/30 text-emerald-300 text-xs font-medium">
                    🔒 AES-256 Encrypted Tokens • Zero-Markup BYOK Gemini
                </div>
                <h1 class="text-3xl sm:text-5xl font-bold tracking-tight text-white">
                    Turn <span class="text-sky-400">Google Tasks</span> into an Active Accountability Coach
                </h1>
                <p class="text-slate-400 text-base sm:text-lg leading-relaxed">
                    Connect your Google account in 1 click, plug in your own free Gemini API key, and get proactive streak reminders & instant natural-language task completion on Telegram or WhatsApp.
                </p>

                <div class="pt-2 flex flex-col sm:flex-row items-center justify-center gap-3">
                    <a id="connect-google-btn" href="/auth/google/login"
                       class="w-full sm:w-auto inline-flex items-center justify-center gap-3 px-6 py-3.5 rounded-2xl bg-white hover:bg-slate-100 text-slate-900 font-semibold text-sm shadow-xl transition">
                        <svg class="w-5 h-5" viewBox="0 0 24 24"><path fill="#4285F4" d="M23.745 12.27c0-.7-.06-1.4-.19-2.07H12v4.51h6.6c-.29 1.52-1.14 2.82-2.4 3.68v3.05h3.88c2.27-2.09 3.665-5.17 3.665-9.17Z"/><path fill="#34A853" d="M12 24c3.24 0 5.95-1.08 7.93-2.91l-3.88-3.05c-1.08.72-2.45 1.16-4.05 1.16-3.12 0-5.77-2.11-6.72-4.96H1.29v3.14C3.26 21.3 7.31 24 12 24Z"/><path fill="#FBBC05" d="M5.28 14.24c-.24-.72-.38-1.49-.38-2.24s.14-1.52.38-2.24V6.62H1.29C.47 8.24 0 10.06 0 12s.47 3.76 1.29 5.38l3.99-3.14Z"/><path fill="#EA4335" d="M12 4.75c1.77 0 3.35.61 4.6 1.8l3.42-3.42C17.95 1.19 15.24 0 12 0 7.31 0 3.26 2.7 1.29 6.62l3.99 3.14c.95-2.85 3.6-4.96 6.72-4.96Z"/></svg>
                        Connect with Google Tasks
                    </a>
                </div>

                <div id="oauth-config-warning" class="hidden max-w-xl mx-auto text-left bg-amber-500/10 border border-amber-500/30 rounded-2xl p-4 text-xs text-amber-200 space-y-1.5">
                    <div class="font-semibold text-amber-300">Admin Setup Needed: Google Web OAuth Client</div>
                    <p>Set <code class="bg-slate-900/70 px-1.5 py-0.5 rounded">GOOGLE_WEB_CLIENT_ID</code> and <code class="bg-slate-900/70 px-1.5 py-0.5 rounded">GOOGLE_WEB_CLIENT_SECRET</code> in Render/env, using this exact Authorized Redirect URI:</p>
                    <code class="block bg-slate-950/70 border border-slate-800 rounded-lg px-2.5 py-1.5 text-sky-300 select-all">{safe_cb}</code>
                </div>
            </div>

            <div class="grid md:grid-cols-3 gap-4">
                <div class="glass-card rounded-2xl p-5 space-y-2">
                    <div class="text-sky-400 font-semibold text-sm">1. Isolated Google Tasks</div>
                    <p class="text-xs text-slate-400 leading-relaxed">Your OAuth refresh token is encrypted at rest using Fernet AES-256 and never shared across users.</p>
                </div>
                <div class="glass-card rounded-2xl p-5 space-y-2">
                    <div class="text-emerald-400 font-semibold text-sm">2. Bring Your Own Gemini Key</div>
                    <p class="text-xs text-slate-400 leading-relaxed">Use Google AI Studio's free tier directly. We test your key before saving so your bot always works.</p>
                </div>
                <div class="glass-card rounded-2xl p-5 space-y-2">
                    <div class="text-violet-400 font-semibold text-sm">3. 1-Click Telegram + WhatsApp</div>
                    <p class="text-xs text-slate-400 leading-relaxed">Bind Telegram instantly with a personal deep link or validate your WhatsApp number for habit nudges.</p>
                </div>
            </div>
        </section>

        <!-- VIEW 2: AUTHENTICATED USER WORKSPACE -->
        <section id="app-view" class="hidden space-y-6">
            <!-- Setup checklist bar -->
            <div class="glass-card rounded-2xl p-5 flex flex-col sm:flex-row sm:items-center justify-between gap-4">
                <div>
                    <h2 class="text-lg font-bold text-white" id="welcome-title">Welcome back!</h2>
                    <p class="text-xs text-slate-400">Complete these 3 quick steps to activate automated habit coaching.</p>
                </div>
                <div class="flex flex-wrap gap-2 text-xs">
                    <span id="badge-google" class="px-3 py-1 rounded-full border border-emerald-500/30 bg-emerald-500/10 text-emerald-300">✅ Google Connected</span>
                    <span id="badge-gemini" class="px-3 py-1 rounded-full border border-amber-500/30 bg-amber-500/10 text-amber-300">⏳ Gemini Key Needed</span>
                    <span id="badge-channel" class="px-3 py-1 rounded-full border border-amber-500/30 bg-amber-500/10 text-amber-300">⏳ Link Telegram or WhatsApp</span>
                </div>
            </div>

            <div class="grid lg:grid-cols-12 gap-6">
                <!-- LEFT COLUMN: ONBOARDING & PREFERENCES -->
                <div class="lg:col-span-7 space-y-6">
                    <!-- BYOK GEMINI -->
                    <div class="glass-card rounded-2xl p-6 space-y-4">
                        <div class="flex items-center justify-between">
                            <div>
                                <h3 class="font-semibold text-white">Step 1 — Bring Your Own Gemini API Key</h3>
                                <p class="text-xs text-slate-400">Encrypted with Fernet AES-256 before database storage.</p>
                            </div>
                            <span id="gemini-verified-pill" class="hidden text-xs px-2.5 py-1 rounded-full bg-emerald-500/15 text-emerald-300 border border-emerald-500/30">✅ Verified</span>
                        </div>

                        <div class="bg-slate-800/60 border border-slate-700/70 rounded-xl p-3.5 text-xs text-slate-300 space-y-1">
                            <div class="font-semibold text-sky-300">How to get a free key in 30 seconds:</div>
                            <div>1. Open <a href="https://aistudio.google.com/apikey" target="_blank" class="text-sky-400 underline">aistudio.google.com/apikey</a></div>
                            <div>2. Click <strong>Create API key</strong> (free Gemini tier, no card needed)</div>
                            <div>3. Paste below and click <strong>Verify &amp; Save</strong></div>
                        </div>

                        <div class="flex flex-col sm:flex-row gap-2">
                            <input id="input-gemini-key" type="password" placeholder="AIzaSy..."
                                class="flex-1 bg-slate-900 border border-slate-700 rounded-xl px-4 py-2.5 text-sm text-white focus:outline-none focus:border-sky-500">
                            <button onclick="testGeminiKey()" id="btn-test-gemini"
                                class="px-4 py-2.5 rounded-xl bg-slate-800 hover:bg-slate-700 border border-slate-600 text-xs font-semibold text-white transition">
                                Verify Key
                            </button>
                        </div>
                        <div id="gemini-feedback" class="text-xs hidden"></div>
                    </div>

                    <!-- CHANNELS & SCHEDULE -->
                    <div class="glass-card rounded-2xl p-6 space-y-5">
                        <div>
                            <h3 class="font-semibold text-white">Step 2 — Connect Telegram, WhatsApp &amp; Reminder Schedule</h3>
                            <p class="text-xs text-slate-400">Choose where HabitSync AI nudges you and listens for <code>done</code> replies.</p>
                        </div>

                        <!-- Telegram Deep Link -->
                        <div class="bg-sky-500/5 border border-sky-500/20 rounded-xl p-4 space-y-3">
                            <div class="flex items-center justify-between gap-2">
                                <div>
                                    <div class="text-sm font-semibold text-white">Telegram Instant Binding</div>
                                    <div id="tg-status-text" class="text-xs text-slate-400">Click Connect below and press Start in Telegram.</div>
                                </div>
                                <span id="tg-status-pill" class="text-xs px-2.5 py-1 rounded-full bg-slate-800 text-slate-300 border border-slate-700">Not linked</span>
                            </div>
                            <div class="flex flex-wrap gap-2">
                                <a id="tg-deep-link" href="#" target="_blank"
                                   class="inline-flex items-center gap-2 px-4 py-2 rounded-xl bg-sky-600 hover:bg-sky-500 text-white text-xs font-semibold transition">
                                    ✈️ Connect Telegram Bot
                                </a>
                                <button onclick="loadProfile()" class="px-3 py-2 rounded-xl bg-slate-800 hover:bg-slate-700 text-xs text-slate-200 transition">
                                    Refresh Status
                                </button>
                                <button onclick="regenerateTelegramLink()" class="px-3 py-2 rounded-xl bg-slate-800 hover:bg-slate-700 text-xs text-slate-400 transition">
                                    New Link Token
                                </button>
                            </div>
                        </div>

                        <!-- WhatsApp E.164 -->
                        <div class="space-y-1.5">
                            <label class="block text-xs font-medium text-slate-300">WhatsApp Number (with country code, e.g. +919876543210)</label>
                            <input id="input-whatsapp" type="text" placeholder="+919876543210"
                                class="w-full bg-slate-900 border border-slate-700 rounded-xl px-4 py-2.5 text-sm text-white focus:outline-none focus:border-sky-500">
                            <p class="text-[11px] text-slate-500">Must start with <code>+</code> and country code. Optional if using Telegram only.</p>
                        </div>

                        <!-- Schedule Controls -->
                        <div class="grid sm:grid-cols-2 gap-4">
                            <div>
                                <label class="block text-xs font-medium text-slate-300 mb-1">Active Channel</label>
                                <select id="input-channel" class="w-full bg-slate-900 border border-slate-700 rounded-xl px-3 py-2.5 text-sm text-white">
                                    <option value="telegram">Telegram Only</option>
                                    <option value="whatsapp">WhatsApp Only</option>
                                    <option value="both">Both Telegram &amp; WhatsApp</option>
                                </select>
                            </div>
                            <div>
                                <label class="block text-xs font-medium text-slate-300 mb-1">Reminder Interval</label>
                                <select id="input-interval" class="w-full bg-slate-900 border border-slate-700 rounded-xl px-3 py-2.5 text-sm text-white">
                                    <option value="60">Every 1 hour</option>
                                    <option value="120">Every 2 hours</option>
                                    <option value="240">Every 4 hours</option>
                                    <option value="480">Every 8 hours</option>
                                </select>
                            </div>
                            <div>
                                <label class="block text-xs font-medium text-slate-300 mb-1">Timezone</label>
                                <input id="input-timezone" type="text" value="Asia/Kolkata"
                                    class="w-full bg-slate-900 border border-slate-700 rounded-xl px-3 py-2.5 text-sm text-white">
                            </div>
                            <div class="grid grid-cols-2 gap-2">
                                <div>
                                    <label class="block text-xs font-medium text-slate-300 mb-1">Start Hour</label>
                                    <input id="input-start-hour" type="number" min="0" max="23" value="8"
                                        class="w-full bg-slate-900 border border-slate-700 rounded-xl px-3 py-2.5 text-sm text-white">
                                </div>
                                <div>
                                    <label class="block text-xs font-medium text-slate-300 mb-1">End Hour</label>
                                    <input id="input-end-hour" type="number" min="0" max="23" value="22"
                                        class="w-full bg-slate-900 border border-slate-700 rounded-xl px-3 py-2.5 text-sm text-white">
                                </div>
                            </div>
                        </div>

                        <label class="flex items-center gap-2.5 text-xs text-slate-300 cursor-pointer">
                            <input id="input-enabled" type="checkbox" checked class="rounded border-slate-700 bg-slate-900 text-sky-500">
                            Enable automated 24/7 streak reminders within my active hours
                        </label>

                        <div id="onboarding-feedback" class="text-xs hidden"></div>

                        <div class="flex flex-wrap gap-3">
                            <button onclick="saveOnboarding()" id="btn-save-onboarding"
                                class="px-5 py-2.5 rounded-xl bg-sky-600 hover:bg-sky-500 text-white text-sm font-semibold transition">
                                Save &amp; Activate
                            </button>
                            <button onclick="sendTestNudge()"
                                class="px-4 py-2.5 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-200 text-sm font-medium transition">
                                Send Test Nudge Now
                            </button>
                        </div>
                    </div>
                </div>

                <!-- RIGHT COLUMN: LIVE TASKS & CHAT SIMULATOR -->
                <div class="lg:col-span-5 space-y-6">
                    <div class="glass-card rounded-2xl p-6 space-y-4">
                        <div class="flex items-center justify-between">
                            <h3 class="font-semibold text-white">My Pending Google Tasks</h3>
                            <button onclick="loadTasks()" class="text-xs text-sky-400 hover:underline">Refresh</button>
                        </div>
                        <form onsubmit="addTask(event)" class="flex gap-2">
                            <input id="new-task-title" type="text" placeholder="Add a habit or task..." required
                                class="flex-1 bg-slate-900 border border-slate-700 rounded-xl px-3 py-2 text-xs text-white focus:outline-none focus:border-sky-500">
                            <button type="submit" class="px-3.5 py-2 rounded-xl bg-sky-600 hover:bg-sky-500 text-white text-xs font-semibold">Add</button>
                        </form>
                        <div id="tasks-list" class="space-y-2 max-h-64 overflow-y-auto text-xs text-slate-400">
                            Loading your tasks...
                        </div>
                    </div>

                    <div class="glass-card rounded-2xl p-6 space-y-4">
                        <h3 class="font-semibold text-white">Chat Simulator (Uses Your BYOK Key)</h3>
                        <div id="chat-log" class="bg-slate-950/70 border border-slate-800 rounded-xl p-3 h-48 overflow-y-auto space-y-2 text-xs">
                            <div class="text-slate-400">Try typing <code>tasks</code>, <code>done</code>, or ask your AI coach a question!</div>
                        </div>
                        <form onsubmit="sendChat(event)" class="flex gap-2">
                            <input id="chat-input" type="text" placeholder="Message HabitSync AI..." required
                                class="flex-1 bg-slate-900 border border-slate-700 rounded-xl px-3 py-2 text-xs text-white focus:outline-none focus:border-sky-500">
                            <button type="submit" class="px-4 py-2 rounded-xl bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-semibold">Send</button>
                        </form>
                    </div>
                </div>
            </div>
        </section>
    </main>

    <script>
        let currentUser = null;

        function escHtml(val) {{
            return String(val ?? '')
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#39;');
        }}

        function setFeedback(elId, msg, isError = false) {{
            const el = document.getElementById(elId);
            el.textContent = msg;
            el.className = isError
                ? "text-xs px-3 py-2 rounded-xl bg-rose-500/10 border border-rose-500/30 text-rose-300"
                : "text-xs px-3 py-2 rounded-xl bg-emerald-500/10 border border-emerald-500/30 text-emerald-300";
        }}

        async function loadProfile() {{
            const res = await fetch('/api/saas/me');
            const data = await res.json();

            if (!data.google_web_oauth_ready) {{
                document.getElementById('oauth-config-warning').classList.remove('hidden');
            }}

            if (!data.authenticated) {{
                document.getElementById('landing-view').classList.remove('hidden');
                document.getElementById('app-view').classList.add('hidden');
                document.getElementById('header-user').classList.add('hidden');
                return;
            }}

            currentUser = data.user;
            document.getElementById('landing-view').classList.add('hidden');
            document.getElementById('app-view').classList.remove('hidden');
            const hdr = document.getElementById('header-user');
            hdr.classList.remove('hidden');
            hdr.classList.add('flex');

            document.getElementById('user-email').textContent = currentUser.email;
            document.getElementById('welcome-title').textContent = `Welcome, ${{currentUser.full_name}}!`;
            const voiceMins = Math.floor((currentUser.call_balance_seconds || 0) / 60);
            document.getElementById('voice-mins').textContent = `${{voiceMins}}m`;

            if (currentUser.avatar_url) {{
                const img = document.getElementById('user-avatar');
                img.src = currentUser.avatar_url;
                img.classList.remove('hidden');
            }}

            const gemBadge = document.getElementById('badge-gemini');
            const gemPill = document.getElementById('gemini-verified-pill');
            if (currentUser.setup_progress.gemini_verified) {{
                gemBadge.className = "px-3 py-1 rounded-full border border-emerald-500/30 bg-emerald-500/10 text-emerald-300";
                gemBadge.textContent = "✅ Gemini Verified";
                gemPill.classList.remove('hidden');
                document.getElementById('input-gemini-key').placeholder = currentUser.masked_gemini_key || "Saved (hidden)";
            }} else {{
                gemBadge.className = "px-3 py-1 rounded-full border border-amber-500/30 bg-amber-500/10 text-amber-300";
                gemBadge.textContent = "⏳ Gemini Key Needed";
                gemPill.classList.add('hidden');
            }}

            const chanBadge = document.getElementById('badge-channel');
            if (currentUser.setup_progress.channel_connected) {{
                chanBadge.className = "px-3 py-1 rounded-full border border-emerald-500/30 bg-emerald-500/10 text-emerald-300";
                chanBadge.textContent = "✅ Channel Bound";
            }} else {{
                chanBadge.className = "px-3 py-1 rounded-full border border-amber-500/30 bg-amber-500/10 text-amber-300";
                chanBadge.textContent = "⏳ Link Telegram or WhatsApp";
            }}

            document.getElementById('tg-deep-link').href = currentUser.telegram_deep_link;
            const tgPill = document.getElementById('tg-status-pill');
            const tgText = document.getElementById('tg-status-text');
            if (currentUser.telegram_linked) {{
                tgPill.className = "text-xs px-2.5 py-1 rounded-full bg-emerald-500/15 text-emerald-300 border border-emerald-500/30";
                tgPill.textContent = "Linked ✅";
                tgText.textContent = `Connected Chat ID: ${{currentUser.telegram_chat_id}}`;
            }} else {{
                tgPill.className = "text-xs px-2.5 py-1 rounded-full bg-slate-800 text-slate-300 border border-slate-700";
                tgPill.textContent = "Not linked";
                tgText.textContent = `Click Connect below and send /start to @${{currentUser.telegram_bot_username}}`;
            }}

            document.getElementById('input-whatsapp').value = currentUser.whatsapp_phone || "";
            document.getElementById('input-channel').value = currentUser.notification_channel || "telegram";
            document.getElementById('input-interval').value = String(currentUser.reminder_interval_minutes || 240);
            document.getElementById('input-timezone').value = currentUser.reminder_timezone || "Asia/Kolkata";
            document.getElementById('input-start-hour').value = currentUser.reminder_start_hour ?? 8;
            document.getElementById('input-end-hour').value = currentUser.reminder_end_hour ?? 22;
            document.getElementById('input-enabled').checked = !!currentUser.reminder_enabled;

            await loadTasks();
        }}

        async function testGeminiKey() {{
            const key = document.getElementById('input-gemini-key').value.trim();
            if (!key) {{
                setFeedback('gemini-feedback', 'Enter a Gemini API key to test.', true);
                return;
            }}
            const btn = document.getElementById('btn-test-gemini');
            btn.disabled = true;
            btn.textContent = 'Testing...';
            try {{
                const res = await fetch('/api/saas/validate-gemini', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ api_key: key }})
                }});
                const data = await res.json();
                if (!res.ok) {{
                    setFeedback('gemini-feedback', data.detail || 'Invalid Gemini key.', true);
                }} else {{
                    setFeedback('gemini-feedback', '✅ ' + data.message + ' Click Save & Activate below to store it.');
                }}
            }} finally {{
                btn.disabled = false;
                btn.textContent = 'Verify Key';
            }}
        }}

        async function saveOnboarding() {{
            const body = {{
                whatsapp_phone: document.getElementById('input-whatsapp').value.trim() || null,
                gemini_api_key: document.getElementById('input-gemini-key').value.trim() || null,
                notification_channel: document.getElementById('input-channel').value,
                reminder_interval_minutes: parseInt(document.getElementById('input-interval').value, 10),
                reminder_timezone: document.getElementById('input-timezone').value.trim() || 'Asia/Kolkata',
                reminder_start_hour: parseInt(document.getElementById('input-start-hour').value, 10),
                reminder_end_hour: parseInt(document.getElementById('input-end-hour').value, 10),
                reminder_enabled: document.getElementById('input-enabled').checked
            }};
            const res = await fetch('/api/saas/onboarding', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify(body)
            }});
            const data = await res.json();
            if (!res.ok) {{
                setFeedback('onboarding-feedback', data.detail || 'Could not save settings.', true);
                return;
            }}
            document.getElementById('input-gemini-key').value = '';
            setFeedback('onboarding-feedback', '✅ Settings validated, encrypted, and saved!');
            await loadProfile();
        }}

        async function regenerateTelegramLink() {{
            const res = await fetch('/api/saas/telegram/regenerate-link', {{ method: 'POST' }});
            if (res.ok) await loadProfile();
        }}

        async function loadTasks() {{
            const box = document.getElementById('tasks-list');
            const res = await fetch('/api/saas/tasks');
            const data = await res.json();
            if (!res.ok) {{
                box.innerHTML = `<div class="text-rose-300">${{escHtml(data.detail || 'Failed to load tasks')}}</div>`;
                return;
            }}
            if (!data.tasks.length) {{
                box.innerHTML = `<div class="text-emerald-300">🎉 All tasks completed!</div>`;
                return;
            }}
            box.innerHTML = data.tasks.map(t => `
                <div class="flex items-center justify-between gap-2 bg-slate-900/70 border border-slate-800 rounded-xl px-3 py-2">
                    <span class="text-slate-200 truncate">${{escHtml(t.title)}}</span>
                    <button onclick="completeTask('${{encodeURIComponent(t.id)}}')" class="text-[11px] px-2.5 py-1 rounded-lg bg-emerald-600/20 hover:bg-emerald-600/40 text-emerald-300 border border-emerald-500/30">Done</button>
                </div>
            `).join('');
        }}

        async function addTask(e) {{
            e.preventDefault();
            const inp = document.getElementById('new-task-title');
            const res = await fetch('/api/saas/tasks', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ title: inp.value.trim() }})
            }});
            if (res.ok) {{
                inp.value = '';
                await loadTasks();
            }}
        }}

        async function completeTask(id) {{
            const res = await fetch(`/api/saas/tasks/${{id}}/complete`, {{ method: 'POST' }});
            if (res.ok) await loadTasks();
        }}

        async function sendChat(e) {{
            e.preventDefault();
            const inp = document.getElementById('chat-input');
            const msg = inp.value.trim();
            if (!msg) return;
            inp.value = '';
            const log = document.getElementById('chat-log');
            log.innerHTML += `<div class="text-right text-sky-300"><strong>You:</strong> ${{escHtml(msg)}}</div>`;
            const res = await fetch('/api/saas/chat', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ message: msg }})
            }});
            const data = await res.json();
            log.innerHTML += `<div class="text-slate-200 bg-slate-900/80 rounded-lg p-2"><strong>HabitSync:</strong> ${{escHtml(data.response || data.detail)}}</div>`;
            log.scrollTop = log.scrollHeight;
            if (data.action === 'completed') await loadTasks();
        }}

        async function sendTestNudge() {{
            const res = await fetch('/api/saas/send-nudge', {{ method: 'POST' }});
            const data = await res.json();
            if (!res.ok) {{
                setFeedback('onboarding-feedback', data.detail || 'Failed to dispatch test nudge', true);
                return;
            }}
            setFeedback('onboarding-feedback', 'Nudge result: ' + JSON.stringify(data.dispatches));
        }}

        async function logout() {{
            await fetch('/auth/logout', {{ method: 'POST' }});
            await loadProfile();
        }}

        loadProfile();
    </script>
</body>
</html>"""
