"""Google Drive implementation of the :class:`~jobpilot.interfaces.FileStorage`
Protocol.

``DriveFileStorage`` uploads a generated document to a Drive folder and returns a
:class:`~jobpilot.models.StoredFile` carrying the file's ``web_view_link``. The Drive
client is built lazily (and can be injected in tests), and the whole backend sits
behind the ``FileStorage`` Protocol, so a Cloudflare R2 (or other) backend can
replace it without touching any caller.

Two authentication modes are supported:

* **User OAuth** (used whenever ``DRIVE_REFRESH_TOKEN`` is set): files are created
  as the authorizing Google account, so they count against that account's quota.
  This is the mode for a personal Google account. Obtain the token once with
  ``jobpilot auth-drive``.
* **Service account**: a service account has no storage quota of its own, so this
  only works when the folder lives in a Shared Drive the account belongs to.
"""

from __future__ import annotations

import json
import os
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import parse_qs, urlparse

from jobpilot.config import Settings, get_settings
from jobpilot.logging import get_logger
from jobpilot.models import StoredFile

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

log = get_logger(__name__)

# Full Drive scope: ``drive.file`` only grants access to files this app created, so
# it cannot write into the owner's pre-existing folder (DRIVE_FOLDER_ID).
_SCOPES = ["https://www.googleapis.com/auth/drive"]
_TOKEN_URI = "https://oauth2.googleapis.com/token"  # noqa: S105 - public endpoint, not a secret
_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
DEFAULT_CONSENT_PORT = 8080


class DriveAuthError(RuntimeError):
    """Raised when Drive OAuth cannot be configured or the consent flow fails."""


class DriveClient(Protocol):
    """The minimal surface of the Google Drive v3 client we depend on.

    Declared as a Protocol so tests can inject a fake and production can pass the
    real ``googleapiclient`` resource without a typing shim.
    """

    def files(self) -> Any: ...


def _load_service_account_info(value: str) -> Mapping[str, object]:
    """Accept either a path to a JSON key file or inline JSON (matches checks)."""
    candidate = Path(value)
    raw = candidate.read_text(encoding="utf-8") if candidate.exists() else value
    data: dict[str, object] = json.loads(raw)
    return data


def _guess_mime_type(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        return "application/pdf"
    return "application/octet-stream"


def uses_oauth(settings: Settings) -> bool:
    """Return whether Drive should authenticate as the user (OAuth refresh token)."""
    return bool(settings.drive_refresh_token)


def build_drive_credentials(settings: Settings) -> Any:
    """Build Google credentials for Drive from OAuth when configured, else a service account.

    OAuth needs the refresh token plus the OAuth client id and secret; the access
    token is minted on demand from the refresh token, so nothing short-lived is
    stored. Imports are lazy because the Google libraries are heavy.
    """
    if uses_oauth(settings):
        client_id = settings.drive_oauth_client_id
        client_secret = settings.drive_oauth_client_secret
        if not client_id or not client_secret:
            raise DriveAuthError(
                "Drive OAuth needs GOOGLE_OAUTH_CLIENT_ID/SECRET (or GMAIL_CLIENT_ID/SECRET)"
            )
        from google.oauth2.credentials import Credentials  # noqa: PLC0415 - lazy dep

        return Credentials(
            token=None,
            refresh_token=settings.drive_refresh_token,
            client_id=client_id,
            client_secret=client_secret,
            token_uri=_TOKEN_URI,
            scopes=_SCOPES,
        )

    if not settings.google_service_account_json:
        raise ValueError(
            "Drive is not configured: set DRIVE_REFRESH_TOKEN (OAuth, run "
            "`jobpilot auth-drive`) or GOOGLE_SERVICE_ACCOUNT_JSON"
        )
    from google.oauth2 import service_account  # noqa: PLC0415 - lazy dep

    info = _load_service_account_info(settings.google_service_account_json)
    return service_account.Credentials.from_service_account_info(info, scopes=_SCOPES)


def build_drive_client(settings: Settings) -> DriveClient:
    """Build a real Drive v3 client for the configured auth mode (lazy imports)."""
    from googleapiclient.discovery import build  # noqa: PLC0415 - optional, lazy dep
    from googleapiclient.http import MediaFileUpload  # noqa: F401, PLC0415 - import check

    creds = build_drive_credentials(settings)
    client: DriveClient = build("drive", "v3", credentials=creds, cache_discovery=False)
    return client


_SUCCESS_PAGE = (
    b"<html><body style='font-family:sans-serif'><h2>JobPilot: authorization received."
    b"</h2><p>You can close this tab and return to the terminal.</p></body></html>"
)


class _RedirectCapture(HTTPServer):
    """HTTP server that remembers the first request carrying an OAuth result."""

    redirect_path: str | None = None


class _RedirectHandler(BaseHTTPRequestHandler):
    server: _RedirectCapture

    def do_GET(self) -> None:
        params = parse_qs(urlparse(self.path).query)
        if "code" in params or "error" in params:
            self.server.redirect_path = self.path
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_SUCCESS_PAGE)
        else:
            # favicon, probes and other stray requests: ignore and keep listening.
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        return None


def wait_for_redirect(port: int, timeout: float = 300.0) -> str:
    """Listen on ``localhost:port`` until the OAuth redirect arrives; return its URL.

    Unlike a one-shot server, this keeps serving until a request that actually carries
    the authorization ``code`` (or an ``error``) shows up. Browsers sometimes send
    unrelated or malformed requests first (an HTTPS handshake to a plain-HTTP port, a
    favicon fetch); a one-shot server would spend its single request on that junk and
    fail. Junk is answered and ignored here.
    """
    server = _RedirectCapture(("localhost", port), _RedirectHandler)
    server.timeout = 1.0
    deadline = time.monotonic() + timeout
    try:
        while server.redirect_path is None:
            if time.monotonic() > deadline:
                raise DriveAuthError(
                    f"Timed out after {int(timeout)}s waiting for the Google redirect."
                )
            server.handle_request()
    finally:
        server.server_close()
    return f"http://localhost:{port}{server.redirect_path}"


def obtain_refresh_token(
    client_id: str,
    client_secret: str,
    *,
    port: int = DEFAULT_CONSENT_PORT,
    flow_factory: Callable[[dict[str, Any], list[str]], Any] | None = None,
    open_url: Callable[[str], object] | None = None,
    announce: Callable[[str], object] | None = None,
    wait: Callable[[int], str] | None = None,
) -> str:
    """Run the one-time interactive OAuth consent and return a Drive refresh token.

    Opens the browser on Google's consent page with a local redirect
    (``http://localhost:<port>/``). The owner signs in with the account that should own
    the uploaded files and grants Drive access. ``access_type=offline`` plus the
    ``consent`` prompt forces Google to issue a refresh token even if the app was
    authorized before, and ``select_account`` always shows the account chooser. The
    browser, the redirect listener and the OAuth flow are injectable so tests never
    open a browser or a socket.
    """
    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": _AUTH_URI,
            "token_uri": _TOKEN_URI,
            "redirect_uris": [f"http://localhost:{port}/"],
        }
    }
    if flow_factory is None:
        from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: PLC0415 - lazy dep

        def flow_factory(config: dict[str, Any], scopes: list[str]) -> Any:
            return InstalledAppFlow.from_client_config(config, scopes=scopes)

    if open_url is None:
        open_url = webbrowser.open
    if wait is None:
        wait = wait_for_redirect

    flow = flow_factory(client_config, _SCOPES)
    flow.redirect_uri = f"http://localhost:{port}/"
    auth_url, _state = flow.authorization_url(
        access_type="offline", prompt="select_account consent"
    )
    if announce is not None:
        announce(f"If the browser does not open, visit this URL:\n{auth_url}")
    open_url(auth_url)

    redirect_url = wait(port)
    if "error=" in redirect_url and "code=" not in redirect_url:
        raise DriveAuthError(f"Google returned an error: {urlparse(redirect_url).query}")

    # oauthlib refuses a non-HTTPS redirect unless this is set; the redirect is to
    # localhost on this machine, which is what Google allows for installed apps.
    previous = os.environ.get("OAUTHLIB_INSECURE_TRANSPORT")
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
    try:
        flow.fetch_token(authorization_response=redirect_url)
    finally:
        if previous is None:
            os.environ.pop("OAUTHLIB_INSECURE_TRANSPORT", None)
        else:
            os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = previous

    token = getattr(flow.credentials, "refresh_token", None)
    if not token:
        raise DriveAuthError(
            "Google did not return a refresh token. Revoke the app's access at "
            "https://myaccount.google.com/permissions and run the consent again."
        )
    return str(token)


class DriveFileStorage:
    """Upload documents to Google Drive and return their ``web_view_link``."""

    def __init__(
        self,
        *,
        folder_id: str,
        client: DriveClient | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._folder_id = folder_id
        self._client = client
        self._settings = settings

    @classmethod
    def from_settings(
        cls,
        settings: Settings | None = None,
        *,
        client: DriveClient | None = None,
    ) -> DriveFileStorage:
        """Build from :class:`Settings`, requiring ``DRIVE_FOLDER_ID``."""
        settings = settings or get_settings()
        if not settings.drive_folder_id:
            raise ValueError("DRIVE_FOLDER_ID is not configured")
        return cls(folder_id=settings.drive_folder_id, client=client, settings=settings)

    def _ensure_client(self) -> DriveClient:
        if self._client is None:
            self._client = build_drive_client(self._settings or get_settings())
        return self._client

    def upload(self, path: Path, name: str) -> StoredFile:
        """Upload ``path`` to the configured folder and return its stored metadata."""
        from googleapiclient.http import MediaFileUpload  # noqa: PLC0415 - optional, lazy dep

        client = self._ensure_client()
        metadata = {"name": name, "parents": [self._folder_id]}
        media = MediaFileUpload(str(path), mimetype=_guess_mime_type(path), resumable=False)
        created = (
            client.files()
            .create(
                body=metadata,
                media_body=media,
                fields="id,name,webViewLink",
                supportsAllDrives=True,
            )
            .execute()
        )
        stored = StoredFile(
            file_id=str(created["id"]),
            name=str(created.get("name", name)),
            web_view_link=str(created["webViewLink"]),
        )
        log.info("drive_upload_done", name=stored.name, file_id=stored.file_id)
        return stored
