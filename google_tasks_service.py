import json
import logging
import os
import ssl
import threading
import time
from typing import List, Dict, Optional, Any
import requests
from google.auth.transport.requests import Request, AuthorizedSession
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
import httplib2
import google_auth_httplib2
from googleapiclient.discovery import build

from config import BASE_DIR, config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("GoogleTasksService")

SCOPES = ["https://www.googleapis.com/auth/tasks"]
WEB_OAUTH_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/tasks",
]
TASKS_API_BASE = "https://tasks.googleapis.com/tasks/v1"


def get_web_oauth_client_config() -> Dict[str, str]:
    """Resolves Google Web Application OAuth client credentials from env or credentials_web.json."""
    client_id = (config.GOOGLE_WEB_CLIENT_ID or "").strip()
    client_secret = (config.GOOGLE_WEB_CLIENT_SECRET or "").strip()
    if client_id and client_secret:
        return {"client_id": client_id, "client_secret": client_secret}

    web_file = BASE_DIR / "credentials_web.json"
    if web_file.exists():
        data = json.loads(web_file.read_text(encoding="utf-8"))
        section = data.get("web") or data.get("installed") or {}
        if section.get("client_id") and section.get("client_secret"):
            return {
                "client_id": section["client_id"],
                "client_secret": section["client_secret"],
            }

    raise RuntimeError(
        "Google Web OAuth credentials are not configured. Set GOOGLE_WEB_CLIENT_ID and "
        "GOOGLE_WEB_CLIENT_SECRET in environment variables."
    )


class GoogleTasksService:
    def __init__(self):
        self._creds_lock = threading.Lock()
        self._thread_local = threading.local()
        self._creds: Optional[Credentials] = None
        self._auth_in_progress = False
        self._token_invalid = False
        self._last_auth_error: Optional[str] = None

    def _load_creds_from_env_or_file(self) -> Optional[Credentials]:
        """Loads Google OAuth Credentials from local token.json or GOOGLE_TOKEN_JSON env var."""
        token_path = config.get_token_path()
        if token_path.exists():
            try:
                return Credentials.from_authorized_user_file(str(token_path), SCOPES)
            except Exception as e:
                logger.warning(f"Error loading {token_path}: {e}.")

        if config.GOOGLE_TOKEN_JSON.strip():
            try:
                info = json.loads(config.GOOGLE_TOKEN_JSON)
                return Credentials.from_authorized_user_info(info, SCOPES)
            except Exception as e:
                logger.warning(f"Error parsing GOOGLE_TOKEN_JSON env var: {e}.")
        return None

    def _persist_creds(self, creds: Credentials):
        """Persists credentials to memory/env var and local token.json if writable."""
        creds_json = creds.to_json()
        os.environ["GOOGLE_TOKEN_JSON"] = creds_json
        config.GOOGLE_TOKEN_JSON = creds_json

        token_path = config.get_token_path()
        if token_path.exists() or not config.IS_CLOUD:
            try:
                with open(token_path, "w", encoding="utf-8") as token_file:
                    token_file.write(creds_json)
            except OSError as e:
                logger.debug(f"Skipping token.json write in cloud/read-only env: {e}")

    def is_authenticated(self) -> bool:
        """Returns True if a valid or refreshable token exists and has not failed refresh."""
        if self._token_invalid:
            return False
        creds = self._creds or self._load_creds_from_env_or_file()
        if not creds:
            return False
        return bool(creds.valid or (creds.expired and creds.refresh_token))

    def get_last_auth_error(self) -> Optional[str]:
        return self._last_auth_error

    def reset_service(self):
        self._creds = None
        self._token_invalid = False
        self._last_auth_error = None
        self._thread_local = threading.local()

    def save_token_data(self, token_str: str):
        """Validates, tests, and applies a fresh token.json / GOOGLE_TOKEN_JSON string at runtime."""
        info = json.loads(token_str)
        if "refresh_token" not in info and "token" not in info:
            raise ValueError("Invalid token JSON! Must contain 'refresh_token' or 'token'.")

        creds = Credentials.from_authorized_user_info(info, SCOPES)
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                raise ValueError("Provided token is expired and has no valid refresh_token.")

        with self._creds_lock:
            self._creds = creds
            self._token_invalid = False
            self._last_auth_error = None
            self._thread_local = threading.local()
            self._persist_creds(creds)
        logger.info("Updated Google Tasks token successfully at runtime.")

    def save_credentials_file(self, content_str: str):
        """Validates and saves credentials.json (and GOOGLE_CREDENTIALS_JSON in memory)."""
        data = json.loads(content_str)
        if "installed" not in data and "web" not in data:
            raise ValueError("Invalid credentials.json format! Must contain 'installed' or 'web' client configuration.")

        os.environ["GOOGLE_CREDENTIALS_JSON"] = content_str
        config.GOOGLE_CREDENTIALS_JSON = content_str

        cred_path = config.get_credentials_path()
        try:
            with open(cred_path, "w", encoding="utf-8") as f:
                f.write(content_str)
            logger.info(f"Saved credentials to {cred_path}")
        except OSError as e:
            logger.warning(f"Could not write {cred_path} (using env var in memory): {e}")
        self.reset_service()

    def get_credentials(self, force_refresh: bool = False) -> Credentials:
        """Loads valid credentials, refreshes if expired, or runs local OAuth flow (thread-safe)."""
        with self._creds_lock:
            cred_path = config.get_credentials_path()

            creds = self._creds
            if creds is None:
                creds = self._load_creds_from_env_or_file()

            if not creds or not creds.valid or force_refresh:
                if creds and (creds.expired or force_refresh) and creds.refresh_token:
                    logger.info("Refreshing Google Tasks access token...")
                    try:
                        creds.refresh(Request())
                        self._token_invalid = False
                        self._last_auth_error = None
                    except Exception as e:
                        logger.warning(f"Token refresh failed: {e}.")
                        self._creds = None
                        self._token_invalid = True
                        self._last_auth_error = str(e)
                        creds = None

                if not creds:
                    if config.IS_CLOUD:
                        raise RuntimeError(
                            "Google Tasks token expired or revoked! Publish your OAuth app in Google Cloud Console "
                            "(Testing tokens expire every 7 days), re-authorize locally with `cli_test.py --auth`, "
                            "and paste/upload the new token in Settings or GOOGLE_TOKEN_JSON."
                        )

                    if cred_path.exists():
                        logger.info(f"Starting local OAuth flow using {cred_path}...")
                        flow = InstalledAppFlow.from_client_secrets_file(str(cred_path), SCOPES)
                    elif config.GOOGLE_CREDENTIALS_JSON.strip():
                        logger.info("Starting local OAuth flow using GOOGLE_CREDENTIALS_JSON...")
                        flow = InstalledAppFlow.from_client_config(
                            json.loads(config.GOOGLE_CREDENTIALS_JSON), SCOPES
                        )
                    else:
                        raise FileNotFoundError(
                            f"Google credentials not found at {cred_path} or GOOGLE_CREDENTIALS_JSON. "
                            "Please upload credentials.json first."
                        )
                    creds = flow.run_local_server(port=0, prompt="consent")

                self._token_invalid = False
                self._last_auth_error = None
                self._persist_creds(creds)

            self._creds = creds
            return creds

    def trigger_oauth_flow_async(self) -> dict:
        """Triggers the OAuth flow in a background thread so the UI does not block."""
        if config.IS_CLOUD:
            return {
                "status": "error",
                "message": "Browser OAuth cannot launch inside a headless cloud container. Generate token.json locally and set GOOGLE_TOKEN_JSON in your cloud environment variables."
            }

        if self._auth_in_progress:
            return {"status": "in_progress", "message": "Authentication flow is already running in browser."}

        if not config.has_google_credentials():
            return {"status": "error", "message": "credentials.json is missing! Upload it first."}

        def _run_flow():
            self._auth_in_progress = True
            try:
                logger.info("Launching OAuth browser server...")
                self.reset_service()
                self.get_credentials()
                logger.info("OAuth browser flow finished successfully.")
            except Exception as e:
                logger.error(f"Error in async OAuth flow: {e}")
            finally:
                self._auth_in_progress = False

        thread = threading.Thread(target=_run_flow, daemon=True)
        thread.start()
        return {"status": "started", "message": "Google sign-in opened in your default browser. Please click Allow."}

    def _get_session(self, force_new: bool = False) -> AuthorizedSession:
        """Returns a thread-local requests-based AuthorizedSession (avoids shared httplib2 SSL corruption)."""
        session = getattr(self._thread_local, "session", None)
        if session is None or force_new:
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass
            creds = self.get_credentials()
            session = AuthorizedSession(creds)
            self._thread_local.session = session
        return session

    def _request(self, method: str, path: str, **kwargs) -> Dict[str, Any]:
        """Executes a Google Tasks REST call with automatic retry on transient SSL/socket errors."""
        url = f"{TASKS_API_BASE}{path}"
        kwargs.setdefault("timeout", 15)

        last_err: Optional[Exception] = None
        for attempt in range(3):
            force_new = attempt > 0
            session = self._get_session(force_new=force_new)
            try:
                resp = session.request(method, url, **kwargs)
                if resp.status_code == 401 and attempt < 2:
                    logger.warning("Google Tasks returned 401; refreshing credentials and retrying...")
                    self.get_credentials(force_refresh=True)
                    self._thread_local.session = None
                    continue
                resp.raise_for_status()
                return resp.json() if resp.content else {}
            except (
                ssl.SSLError,
                requests.exceptions.SSLError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                OSError,
            ) as e:
                last_err = e
                logger.warning(f"Transient network/SSL error on Google Tasks ({method} {path}, attempt {attempt + 1}/3): {e}")
                self._thread_local.session = None
                time.sleep(0.25 * (attempt + 1))

        raise last_err if last_err else RuntimeError("Google Tasks request failed")

    def get_service(self):
        """Returns a thread-local authorized Google Tasks discovery resource."""
        service = getattr(self._thread_local, "service", None)
        if service is None:
            creds = self.get_credentials()
            authed_http = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=15))
            service = build("tasks", "v1", http=authed_http)
            self._thread_local.service = service
        return service

    def list_task_lists(self) -> List[Dict[str, Any]]:
        results = self._request("GET", "/users/@me/lists", params={"maxResults": 100})
        return results.get("items", [])

    def get_pending_tasks(self, task_list_id: str = "@default") -> List[Dict[str, Any]]:
        results = self._request(
            "GET",
            f"/lists/{task_list_id}/tasks",
            params={
                "showCompleted": "false",
                "showHidden": "false",
                "maxResults": 100,
            },
        )

        items = results.get("items", [])
        pending = []
        for item in items:
            if item.get("status") == "needsAction":
                pending.append({
                    "id": item.get("id"),
                    "title": item.get("title", "Untitled Task"),
                    "notes": item.get("notes", ""),
                    "due": item.get("due", None),
                    "updated": item.get("updated", None),
                    "status": item.get("status", "needsAction")
                })
        return pending

    def complete_task(self, task_id: str, task_list_id: str = "@default") -> Dict[str, Any]:
        updated_task = self._request(
            "PATCH",
            f"/lists/{task_list_id}/tasks/{task_id}",
            json={"id": task_id, "status": "completed"},
        )
        logger.info(f"Marked task '{updated_task.get('title')}' ({task_id}) as completed.")
        return updated_task

    def create_task(self, title: str, notes: Optional[str] = None, due: Optional[str] = None, task_list_id: str = "@default") -> Dict[str, Any]:
        body: Dict[str, Any] = {"title": title}
        if notes:
            body["notes"] = notes
        if due:
            body["due"] = due

        result = self._request("POST", f"/lists/{task_list_id}/tasks", json=body)
        logger.info(f"Created new task: '{title}' ({result.get('id')})")
        return result


class UserGoogleTasksClient(GoogleTasksService):
    """Per-user isolated Google Tasks client backed by a decrypted SaaS refresh token."""

    def __init__(
        self,
        refresh_token: str,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
    ):
        super().__init__()
        if not refresh_token:
            raise ValueError("Missing Google Tasks refresh token for user")
        self._refresh_token = refresh_token
        self._client_id = client_id
        self._client_secret = client_secret

    def get_credentials(self, force_refresh: bool = False) -> Credentials:
        """Builds and refreshes user-scoped OAuth credentials in memory without touching disk."""
        with self._creds_lock:
            if self._creds and self._creds.valid and not force_refresh:
                return self._creds

            client_id = self._client_id
            client_secret = self._client_secret
            if not client_id or not client_secret:
                web_cfg = get_web_oauth_client_config()
                client_id = client_id or web_cfg["client_id"]
                client_secret = client_secret or web_cfg["client_secret"]

            if self._creds is None:
                self._creds = Credentials(
                    token=None,
                    refresh_token=self._refresh_token,
                    token_uri="https://oauth2.googleapis.com/token",
                    client_id=client_id,
                    client_secret=client_secret,
                    scopes=SCOPES,
                )

            if not self._creds.valid or force_refresh:
                self._creds.refresh(Request())
                self._token_invalid = False
                self._last_auth_error = None

            return self._creds


tasks_service = GoogleTasksService()
