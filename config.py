import os
from pathlib import Path
from dotenv import load_dotenv

# Base directory of the project
BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
ENV_EXAMPLE_PATH = BASE_DIR / ".env.example"

# Load local .env if present (in cloud platforms, environment variables are injected directly)
if ENV_PATH.exists():
    load_dotenv(ENV_PATH)


def _detect_public_base_url() -> str:
    """Detects explicitly configured PUBLIC_BASE_URL or platform-injected Render/Railway URLs."""
    explicit = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if explicit:
        return explicit

    render_url = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
    if render_url:
        return render_url

    railway_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip().rstrip("/")
    if railway_domain:
        return railway_domain if railway_domain.startswith("http") else f"https://{railway_domain}"

    return ""


class Config:
    def __init__(self):
        self.reload()

    def reload(self):
        if ENV_PATH.exists():
            load_dotenv(ENV_PATH, override=True)

        self.GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
        self.GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")

        self.TWILIO_ACCOUNT_SID: str = os.getenv("TWILIO_ACCOUNT_SID", "")
        self.TWILIO_AUTH_TOKEN: str = os.getenv("TWILIO_AUTH_TOKEN", "")
        self.TWILIO_WHATSAPP_NUMBER: str = os.getenv("TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155238886")
        self.TWILIO_CONTENT_SID: str = os.getenv("TWILIO_CONTENT_SID", "HXfe5ab5f00277942d4d4200328b4d403c")
        self.USER_WHATSAPP_NUMBER: str = os.getenv("USER_WHATSAPP_NUMBER", "")

        # Telegram Configuration
        self.TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")
        self.TELEGRAM_WEBHOOK_SECRET: str = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")

        # Active Channel Selection: "telegram", "whatsapp", or "both"
        self.NOTIFICATION_CHANNEL: str = os.getenv("NOTIFICATION_CHANNEL", "both").lower()

        # Google Tasks OAuth (file paths for local, JSON env vars for ephemeral cloud hosts)
        self.GOOGLE_CREDENTIALS_FILE: str = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
        self.GOOGLE_TOKEN_FILE: str = os.getenv("GOOGLE_TOKEN_FILE", "token.json")
        self.GOOGLE_CREDENTIALS_JSON: str = os.getenv("GOOGLE_CREDENTIALS_JSON", "")
        self.GOOGLE_TOKEN_JSON: str = os.getenv("GOOGLE_TOKEN_JSON", "")

        # Cloud & Automated Scheduler Settings
        self.PUBLIC_BASE_URL: str = _detect_public_base_url()
        self.DASHBOARD_PASSWORD: str = os.getenv("DASHBOARD_PASSWORD", "")

        # Phase 2: Multi-Tenant SaaS Database, Encryption & Google Web OAuth
        self.DATABASE_URL: str = os.getenv("DATABASE_URL", "")
        self.ENCRYPTION_KEY: str = os.getenv("ENCRYPTION_KEY", "")
        self.APP_SECRET_KEY: str = os.getenv("APP_SECRET_KEY", "")
        self.SAAS_MODE: bool = os.getenv("SAAS_MODE", "false").lower() in ("1", "true", "yes")
        self.GOOGLE_WEB_CLIENT_ID: str = os.getenv("GOOGLE_WEB_CLIENT_ID", "")
        self.GOOGLE_WEB_CLIENT_SECRET: str = os.getenv("GOOGLE_WEB_CLIENT_SECRET", "")
        self.TELEGRAM_BOT_USERNAME: str = os.getenv("TELEGRAM_BOT_USERNAME", "HabitSyncShivanshBot").lstrip("@")
        self.IS_CLOUD: bool = bool(
            os.getenv("RENDER")
            or os.getenv("RAILWAY_ENVIRONMENT")
            or os.getenv("K_SERVICE")
            or os.getenv("FLY_APP_NAME")
            or os.getenv("CLOUD_ENV")
        )

        self.REMINDER_ENABLED: bool = os.getenv("REMINDER_ENABLED", "false").lower() in ("1", "true", "yes", "on")
        self.REMINDER_INTERVAL_MINUTES: int = max(15, int(os.getenv("REMINDER_INTERVAL_MINUTES", "240")))
        self.REMINDER_TIMEZONE: str = os.getenv("REMINDER_TIMEZONE", "Asia/Kolkata")
        self.REMINDER_START_HOUR: int = int(os.getenv("REMINDER_START_HOUR", "8"))
        self.REMINDER_END_HOUR: int = int(os.getenv("REMINDER_END_HOUR", "22"))

        self.PORT: int = int(os.getenv("PORT", "8000"))
        self.HOST: str = os.getenv("HOST", "0.0.0.0")

    def get_credentials_path(self) -> Path:
        path = Path(self.GOOGLE_CREDENTIALS_FILE)
        return path if path.is_absolute() else BASE_DIR / path

    def get_token_path(self) -> Path:
        path = Path(self.GOOGLE_TOKEN_FILE)
        return path if path.is_absolute() else BASE_DIR / path

    def has_google_credentials(self) -> bool:
        return bool(self.GOOGLE_CREDENTIALS_JSON.strip()) or self.get_credentials_path().exists()

    def has_google_token(self) -> bool:
        return bool(self.GOOGLE_TOKEN_JSON.strip()) or self.get_token_path().exists()

    def has_google_web_oauth(self) -> bool:
        return bool(self.GOOGLE_WEB_CLIENT_ID.strip() and self.GOOGLE_WEB_CLIENT_SECRET.strip()) or (
            BASE_DIR / "credentials_web.json"
        ).exists()

    def validate(self) -> dict:
        """Checks which configuration components are ready and which are missing."""
        return {
            "gemini": bool(self.GEMINI_API_KEY and not self.GEMINI_API_KEY.startswith("your_")),
            "twilio": bool(
                self.TWILIO_ACCOUNT_SID
                and self.TWILIO_AUTH_TOKEN
                and not self.TWILIO_ACCOUNT_SID.startswith("your_")
            ),
            "user_phone": bool(
                self.USER_WHATSAPP_NUMBER
                and not self.USER_WHATSAPP_NUMBER.endswith("XXXXXXXXXX")
            ),
            "telegram": bool(
                self.TELEGRAM_BOT_TOKEN
                and not self.TELEGRAM_BOT_TOKEN.startswith("your_")
            ),
            "telegram_chat_id": bool(self.TELEGRAM_CHAT_ID),
            "channel": self.NOTIFICATION_CHANNEL,
            "google_credentials_exists": self.has_google_credentials(),
            "google_token_exists": self.has_google_token(),
            "google_web_oauth_ready": self.has_google_web_oauth(),
            "saas_mode": self.SAAS_MODE,
            "is_cloud": self.IS_CLOUD,
            "reminder_enabled": self.REMINDER_ENABLED,
        }

    def save_settings(self, updates: dict):
        """Updates runtime environment (and local .env if running locally)."""
        for k, v in updates.items():
            if v is not None:
                os.environ[k] = str(v)

        # In cloud containers without .env, do NOT create a .env from .env.example placeholders
        if ENV_PATH.exists() or not self.IS_CLOUD:
            target_path = ENV_PATH if ENV_PATH.exists() else ENV_EXAMPLE_PATH
            lines = []
            if target_path.exists():
                with open(target_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()

            existing_keys = set()
            new_lines = []

            for line in lines:
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    k, _ = stripped.split("=", 1)
                    k = k.strip()
                    existing_keys.add(k)
                    if k in updates and updates[k] is not None:
                        new_lines.append(f"{k}={updates[k]}\n")
                    else:
                        new_lines.append(line)
                else:
                    new_lines.append(line)

            for k, v in updates.items():
                if k not in existing_keys and v is not None:
                    new_lines.append(f"{k}={v}\n")

            try:
                with open(ENV_PATH, "w", encoding="utf-8") as f:
                    f.writelines(new_lines)
            except OSError:
                # Read-only cloud container filesystem; runtime os.environ is already updated
                pass

        self.reload()


config = Config()
