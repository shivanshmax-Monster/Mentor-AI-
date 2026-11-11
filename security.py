import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from pathlib import Path
from typing import Any, Dict, Optional

from cryptography.fernet import Fernet, InvalidToken

from config import BASE_DIR, config

logger = logging.getLogger("Security")

SAAS_COOKIE_NAME = "habitsync_user_session"
OAUTH_STATE_COOKIE_NAME = "habitsync_oauth_state"

_LOCAL_FERNET_FILE = BASE_DIR / ".local_fernet.key"
_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")
_EPHEMERAL_SECRET: Optional[bytes] = None


def generate_fernet_key() -> str:
    """Generates a fresh 32-byte URL-safe base64 Fernet encryption key."""
    return Fernet.generate_key().decode("ascii")


def _load_or_create_local_fernet_key() -> str:
    """Persists a local dev Fernet key so encrypted dev records survive restarts."""
    if _LOCAL_FERNET_FILE.exists():
        stored = _LOCAL_FERNET_FILE.read_text(encoding="utf-8").strip()
        if stored:
            return stored
    key = generate_fernet_key()
    _LOCAL_FERNET_FILE.write_text(key, encoding="utf-8")
    return key


def _get_fernet() -> Fernet:
    """Returns a configured Fernet instance using ENCRYPTION_KEY (or local dev key)."""
    raw_key = (config.ENCRYPTION_KEY or "").strip()
    if not raw_key:
        if config.IS_CLOUD:
            raise RuntimeError(
                "ENCRYPTION_KEY is missing in cloud environment! "
                "Generate one via `python cli_test.py --generate-key` and set ENCRYPTION_KEY."
            )
        raw_key = _load_or_create_local_fernet_key()

    try:
        return Fernet(raw_key.encode("ascii"))
    except Exception:
        # Derive a valid 32-byte URL-safe base64 key if user supplied an arbitrary passphrase
        derived = base64.urlsafe_b64encode(hashlib.sha256(raw_key.encode("utf-8")).digest())
        return Fernet(derived)


def encrypt_secret(plaintext: Optional[str]) -> Optional[str]:
    """Encrypts sensitive tokens/API keys using AES-128-CBC + HMAC-SHA256 (Fernet)."""
    if plaintext is None or plaintext == "":
        return None
    token = _get_fernet().encrypt(plaintext.encode("utf-8"))
    return token.decode("ascii")


def decrypt_secret(ciphertext: Optional[str]) -> Optional[str]:
    """Decrypts a Fernet-encrypted secret back into plaintext."""
    if ciphertext is None or ciphertext == "":
        return None
    try:
        plaintext = _get_fernet().decrypt(ciphertext.encode("ascii"))
        return plaintext.decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("Unable to decrypt stored secret (key mismatch or corrupted ciphertext)") from exc


def _get_signing_secret() -> bytes:
    """Returns the HMAC signing secret for user session & OAuth state cookies."""
    global _EPHEMERAL_SECRET
    candidate = (
        (config.APP_SECRET_KEY or "").strip()
        or (config.ENCRYPTION_KEY or "").strip()
        or (config.DASHBOARD_PASSWORD or "").strip()
    )
    if candidate:
        return hashlib.sha256(f"habitsync-cookie:{candidate}".encode("utf-8")).digest()

    if not config.IS_CLOUD:
        local_key = _load_or_create_local_fernet_key()
        return hashlib.sha256(f"habitsync-local-cookie:{local_key}".encode("utf-8")).digest()

    if _EPHEMERAL_SECRET is None:
        logger.warning("APP_SECRET_KEY is unset in cloud mode; falling back to ephemeral cookie secret.")
        _EPHEMERAL_SECRET = secrets.token_bytes(32)
    return _EPHEMERAL_SECRET


def sign_payload(data: Dict[str, Any], ttl_seconds: int = 86400 * 30) -> str:
    """Serializes and HMAC-SHA256 signs a JSON payload with expiration."""
    envelope = dict(data)
    envelope["exp"] = int(time.time()) + ttl_seconds
    raw_json = json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode("utf-8")
    b64_payload = base64.urlsafe_b64encode(raw_json).decode("ascii").rstrip("=")
    sig = hmac.new(_get_signing_secret(), b64_payload.encode("ascii"), hashlib.sha256).digest()
    b64_sig = base64.urlsafe_b64encode(sig).decode("ascii").rstrip("=")
    return f"{b64_payload}.{b64_sig}"


def verify_signed_payload(token: Optional[str]) -> Optional[Dict[str, Any]]:
    """Verifies an HMAC-SHA256 signed payload and checks expiration."""
    if not token or "." not in token:
        return None
    try:
        b64_payload, b64_sig = token.split(".", 1)
        expected_sig = hmac.new(
            _get_signing_secret(),
            b64_payload.encode("ascii"),
            hashlib.sha256,
        ).digest()
        expected_b64 = base64.urlsafe_b64encode(expected_sig).decode("ascii").rstrip("=")
        if not hmac.compare_digest(b64_sig, expected_b64):
            return None

        padded = b64_payload + "=" * (-len(b64_payload) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return payload
    except Exception:
        return None


def create_user_session_token(user_id: str, ttl_seconds: int = 86400 * 30) -> str:
    """Creates a signed session cookie value bound to a SaaS user ID."""
    return sign_payload({"sub": user_id, "typ": "user_session"}, ttl_seconds=ttl_seconds)


def verify_user_session_token(token: Optional[str]) -> Optional[str]:
    """Extracts and validates the SaaS user ID from a session cookie value."""
    payload = verify_signed_payload(token)
    if not payload or payload.get("typ") != "user_session":
        return None
    user_id = payload.get("sub")
    return str(user_id) if user_id else None


def create_oauth_state_token(nonce: Optional[str] = None, ttl_seconds: int = 600) -> str:
    """Creates a short-lived signed OAuth CSRF state token."""
    return sign_payload({"nonce": nonce or secrets.token_urlsafe(16), "typ": "oauth_state"}, ttl_seconds=ttl_seconds)


def verify_oauth_state_token(token: Optional[str]) -> bool:
    """Validates an OAuth CSRF state cookie/parameter."""
    payload = verify_signed_payload(token)
    return bool(payload and payload.get("typ") == "oauth_state" and payload.get("nonce"))


def normalize_whatsapp_number(phone: Optional[str]) -> str:
    """
    Normalizes and validates a WhatsApp phone number in strict E.164 format.
    Rejects local phone numbers missing a '+' country code.
    """
    if not phone or not str(phone).strip():
        raise ValueError("WhatsApp phone number is required.")

    raw = str(phone).strip()
    if raw.lower().startswith("whatsapp:"):
        raw = raw[len("whatsapp:"):].strip()

    cleaned = re.sub(r"[\s\-()]+", "", raw)
    if not _E164_RE.match(cleaned):
        raise ValueError(
            "Phone number must include a '+' country code and 8-15 digits (e.g., +919876543210)."
        )
    return f"whatsapp:{cleaned}"


def display_phone_from_whatsapp(whatsapp_number: Optional[str]) -> str:
    """Strips the 'whatsapp:' prefix for UI display."""
    if not whatsapp_number:
        return ""
    if whatsapp_number.lower().startswith("whatsapp:"):
        return whatsapp_number[len("whatsapp:"):]
    return whatsapp_number


def generate_link_token() -> str:
    """Generates a compact hex token used for Telegram deep-link binding (/start link_<code>)."""
    return secrets.token_hex(8)


def mask_secret(secret: Optional[str]) -> str:
    """Masks an API key for safe display in responses without revealing the secret."""
    if not secret:
        return ""
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}...{secret[-4:]}"

