import logging
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy.orm import Session

from database import User
from gemini_service import gemini_service
from google_tasks_service import UserGoogleTasksClient
from security import decrypt_secret, generate_link_token, normalize_whatsapp_number
from task_resolver import chat_contexts, execute_completion, resolve_tasks_to_complete

logger = logging.getLogger("TenantRouter")

DEFAULT_LIST_KEYWORDS = ["tasks", "list", "pending", "all tasks", "task list", "meri tasks"]


def resolve_tasks_client_for_user(user: User) -> UserGoogleTasksClient:
    """Constructs a per-user Google Tasks client from their Fernet-encrypted refresh token."""
    if not user.google_refresh_token_enc:
        raise ValueError("Google Tasks is not connected for this account yet.")
    refresh_token = decrypt_secret(user.google_refresh_token_enc)
    if not refresh_token:
        raise ValueError("Google Tasks refresh token is empty.")
    return UserGoogleTasksClient(refresh_token=refresh_token)


def resolve_gemini_key_for_user(user: User) -> Optional[str]:
    """Decrypts the user's BYOK Gemini API key if present."""
    if not user.gemini_api_key_enc:
        return None
    return decrypt_secret(user.gemini_api_key_enc)


def find_user_by_telegram_chat(db: Session, chat_id: str) -> Optional[User]:
    """Finds a SaaS user mapped to a given Telegram chat ID."""
    return db.query(User).filter(User.telegram_chat_id == str(chat_id)).first()


def find_user_by_whatsapp(db: Session, from_number: str) -> Optional[User]:
    """Finds a SaaS user mapped to a given Twilio WhatsApp sender."""
    candidates = [from_number.strip()]
    try:
        candidates.append(normalize_whatsapp_number(from_number))
    except ValueError:
        pass
    for candidate in candidates:
        user = db.query(User).filter(User.whatsapp_number == candidate).first()
        if user:
            return user
    return None


def bind_telegram_by_link_token(db: Session, link_token: str, chat_id: str) -> Optional[User]:
    """Associates a Telegram chat_id with the SaaS user owning `link_token`."""
    cleaned = (link_token or "").strip()
    if not cleaned:
        return None

    user = db.query(User).filter(User.telegram_link_token == cleaned).first()
    if not user:
        return None

    chat_str = str(chat_id)
    others = db.query(User).filter(User.telegram_chat_id == chat_str, User.id != user.id).all()
    for other in others:
        other.telegram_chat_id = None

    user.telegram_chat_id = chat_str
    user.telegram_link_token = generate_link_token()
    db.commit()
    db.refresh(user)
    logger.info(f"Bound Telegram chat {chat_str} to SaaS user {user.email}")
    return user


def process_user_chat_result(
    user: User,
    user_text: str,
    session_key: str,
    list_keywords: Optional[List[str]],
    formatter_fn: Callable[[List[Dict[str, Any]]], str],
) -> Dict[str, Any]:
    """
    Executes task listing, intelligent completion, or BYOK Gemini coaching
    strictly using the authenticated SaaS user's own Google Tasks & Gemini credentials.
    """
    text = (user_text or "").strip()
    normalized = text.lower()
    keywords = list_keywords if list_keywords is not None else DEFAULT_LIST_KEYWORDS
    last_assistant_msg = chat_contexts.get(session_key, "")

    try:
        tasks_client = resolve_tasks_client_for_user(user)
        pending_tasks = tasks_client.get_pending_tasks()
    except Exception as exc:
        logger.error(f"Failed to fetch Google Tasks for {user.email}: {exc}")
        return {
            "action": "error",
            "response": (
                "I couldn't access your Google Tasks right now. "
                "Please reconnect Google in your HabitSync AI portal!"
            ),
        }

    matched_tasks, reason = resolve_tasks_to_complete(text, pending_tasks, last_assistant_msg)
    wants_list = text.startswith("/tasks") or any(k in normalized for k in keywords)
    if wants_list and not matched_tasks:
        response_text = formatter_fn(pending_tasks)
        chat_contexts[session_key] = response_text
        return {"action": "list", "response": response_text}

    if matched_tasks:
        response_text = execute_completion(matched_tasks, pending_tasks, tasks_client=tasks_client)
        chat_contexts[session_key] = response_text
        return {
            "action": "completed",
            "response": response_text,
            "matched_tasks": [t["title"] for t in matched_tasks],
            "reason": reason,
        }

    gemini_key = resolve_gemini_key_for_user(user)
    if not gemini_key:
        return {
            "action": "error",
            "response": (
                "🔑 Add your free Gemini API key in the HabitSync AI dashboard to chat with AI. "
                "You can still use *tasks* or *done* anytime!"
            ),
        }

    try:
        response_text = gemini_service.chat_with_tasks(
            text,
            pending_tasks,
            api_key=gemini_key,
        )
        chat_contexts[session_key] = response_text
        return {"action": "chat", "response": response_text}
    except Exception as exc:
        logger.error(f"BYOK Gemini chat failed for {user.email}: {exc}")
        return {
            "action": "error",
            "response": "I couldn't reach Gemini with your API key right now. Reply *done* to complete a task!",
        }


def process_user_chat_message(
    user: User,
    user_text: str,
    session_key: str,
    list_keywords: Optional[List[str]],
    formatter_fn: Callable[[List[Dict[str, Any]]], str],
) -> str:
    """Convenience wrapper returning only the text reply for messaging webhooks."""
    return process_user_chat_result(
        user=user,
        user_text=user_text,
        session_key=session_key,
        list_keywords=list_keywords,
        formatter_fn=formatter_fn,
    )["response"]
