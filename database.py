import logging
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Generator, Optional

from sqlalchemy import Boolean, DateTime, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from config import BASE_DIR, config

logger = logging.getLogger("Database")


def utcnow() -> datetime:
    """Returns timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


def _normalize_database_url(raw_url: Optional[str]) -> str:
    """Normalizes Postgres URLs for SQLAlchemy 2.0 or defaults to local SQLite."""
    url = (raw_url or "").strip()
    if not url:
        return f"sqlite:///{(BASE_DIR / 'habitsync.db').as_posix()}"
    if url.startswith("postgres://"):
        return "postgresql+psycopg2://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg2://" + url[len("postgresql://"):]
    return url


class Base(DeclarativeBase):
    """Base class for HabitSync AI ORM models."""


class User(Base):
    """Multi-tenant SaaS user record with encrypted per-user credentials."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    full_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    avatar_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    google_sub: Mapped[Optional[str]] = mapped_column(String(255), unique=True, nullable=True)

    # Fernet-encrypted credentials (never stored in plaintext)
    google_refresh_token_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    gemini_api_key_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    gemini_key_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Messaging bindings & scheduling preferences
    whatsapp_number: Mapped[Optional[str]] = mapped_column(String(64), unique=True, index=True, nullable=True)
    telegram_chat_id: Mapped[Optional[str]] = mapped_column(String(64), unique=True, index=True, nullable=True)
    telegram_link_token: Mapped[Optional[str]] = mapped_column(String(64), unique=True, index=True, nullable=True)

    notification_channel: Mapped[str] = mapped_column(String(32), default="telegram", nullable=False)
    reminder_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    reminder_interval_minutes: Mapped[int] = mapped_column(Integer, default=240, nullable=False)
    reminder_timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata", nullable=False)
    reminder_start_hour: Mapped[int] = mapped_column(Integer, default=8, nullable=False)
    reminder_end_hour: Mapped[int] = mapped_column(Integer, default=22, nullable=False)
    last_reminder_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Forward-compatible Part B columns (AI Voice Calling & Balance Metering)
    is_calling_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    call_balance_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    def to_public_dict(self, masked_gemini: str = "") -> Dict[str, Any]:
        """Returns safe profile metadata without exposing decrypted secrets."""
        from security import display_phone_from_whatsapp

        return {
            "id": self.id,
            "email": self.email,
            "full_name": self.full_name or self.email.split("@")[0],
            "avatar_url": self.avatar_url or "",
            "has_google_tasks": bool(self.google_refresh_token_enc),
            "has_gemini_key": bool(self.gemini_api_key_enc),
            "gemini_key_verified": bool(self.gemini_key_verified),
            "masked_gemini_key": masked_gemini,
            "whatsapp_phone": display_phone_from_whatsapp(self.whatsapp_number),
            "whatsapp_linked": bool(self.whatsapp_number),
            "telegram_linked": bool(self.telegram_chat_id),
            "telegram_chat_id": self.telegram_chat_id or "",
            "telegram_link_token": self.telegram_link_token or "",
            "notification_channel": self.notification_channel,
            "reminder_enabled": bool(self.reminder_enabled),
            "reminder_interval_minutes": int(self.reminder_interval_minutes or 240),
            "reminder_timezone": self.reminder_timezone or "Asia/Kolkata",
            "reminder_start_hour": int(self.reminder_start_hour if self.reminder_start_hour is not None else 8),
            "reminder_end_hour": int(self.reminder_end_hour if self.reminder_end_hour is not None else 22),
            "last_reminder_sent_at": self.last_reminder_sent_at.isoformat() if self.last_reminder_sent_at else None,
            "is_calling_enabled": bool(self.is_calling_enabled),
            "call_balance_seconds": int(self.call_balance_seconds or 0),
        }


engine = None
SessionLocal = None
ACTIVE_DATABASE_URL = ""


def configure_database(db_url: Optional[str] = None) -> None:
    """Initializes or rebinds the SQLAlchemy engine and session factory."""
    global engine, SessionLocal, ACTIVE_DATABASE_URL

    resolved_url = _normalize_database_url(db_url if db_url is not None else config.DATABASE_URL)
    if engine is not None:
        engine.dispose()

    engine_kwargs: Dict[str, Any] = {"future": True}
    if resolved_url.startswith("sqlite"):
        engine_kwargs["connect_args"] = {"check_same_thread": False}
    else:
        # Neon/Supabase poolers drop idle TCP sockets; pool_pre_ping prevents stale socket errors.
        engine_kwargs["pool_pre_ping"] = True
        engine_kwargs["pool_recycle"] = 300

    engine = create_engine(resolved_url, **engine_kwargs)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, future=True)
    ACTIVE_DATABASE_URL = resolved_url

    backend_label = "SQLite (local fallback)" if resolved_url.startswith("sqlite") else "PostgreSQL"
    logger.info(f"Database configured using {backend_label}")


def init_db() -> None:
    """Creates database tables if they do not exist."""
    if engine is None:
        configure_database()
    Base.metadata.create_all(bind=engine)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a scoped SQLAlchemy session."""
    if SessionLocal is None:
        init_db()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """Context manager for background workers (scheduler/Telegram/WhatsApp routing)."""
    if SessionLocal is None:
        init_db()
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


configure_database()

