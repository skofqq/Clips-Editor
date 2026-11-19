"""OAuth 2.0 for installed apps: browser sign-in with a loopback redirect and PKCE.

The user signs in on Google's own page in their browser; the application
never sees the Google password. The authorization code comes back to a
short-lived HTTP listener on 127.0.0.1.
"""

from __future__ import annotations

import base64
import hashlib
import html
import http.server
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from core.youtube.errors import AuthCancelled, YouTubeAuthError, YouTubeError

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
# Needed: upload videos, and read which channel the account has.
REQUIRED_SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
)
# Optional: adding captions to an uploaded video (captions.insert) needs it.
CAPTIONS_SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"
SCOPES = REQUIRED_SCOPES + (CAPTIONS_SCOPE,)
SIGN_IN_TIMEOUT_S = 300


@dataclass(frozen=True)
class ClientConfig:
    client_id: str
    client_secret: str

    @classmethod
    def from_file(cls, path: str | Path) -> "ClientConfig":
        """Read a ``client_secret_*.json`` downloaded from Google Cloud Console."""
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise YouTubeError(f"Cannot read the client file: {exc}") from exc
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: Any) -> "ClientConfig":
        if isinstance(data, dict) and "web" in data and "installed" not in data:
            raise YouTubeError(
                "This is a 'Web application' client. Create an OAuth client of type 'Desktop app'."
            )
        block = data.get("installed", data) if isinstance(data, dict) else None
        if not isinstance(block, dict) or not block.get("client_id") or not block.get("client_secret"):
            raise YouTubeError("The file does not contain a client_id and client_secret.")
        return cls(str(block["client_id"]), str(block["client_secret"]))

    def to_dict(self) -> dict[str, str]:
        return {"client_id": self.client_id, "client_secret": self.client_secret}


@dataclass
class Credentials:
    access_token: str
    refresh_token: str
    expires_at: float  # time.time() when the access token expires
    scopes: tuple[str, ...] = SCOPES

    @property
    def expired(self) -> bool:
        return time.time() > self.expires_at - 60


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def auth_url(client: ClientConfig, redirect_uri: str, state: str, challenge: str) -> str:
    query = {
        "client_id": client.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",  # we need a refresh token
        "prompt": "select_account consent",  # lets the user pick the channel / brand account
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(query)}"


_DONE_PAGE = """<!doctype html><meta charset="utf-8"><title>Twitch Shorts Studio</title>
<body style="font-family:Segoe UI,sans-serif;background:#18191c;color:#e8e8ea;padding:48px">
<h2>{title}</h2><p>{text}</p></body>"""


class _Receiver(http.server.HTTPServer):
    result: dict[str, str] | None = None


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
        if "code" not in query and "error" not in query:
            self.send_response(404)
            self.end_headers()
            return
        self.server.result = query  # type: ignore[attr-defined]
        ok = "code" in query
        page = _DONE_PAGE.format(
            title="Connected" if ok else "Sign-in was not completed",
            text="You can close this tab and return to Twitch Shorts Studio."
            if ok else html.escape(query.get("error", "")),
        )
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # silence the default stderr logging
        pass


def sign_in(
    client: ClientConfig,
    open_browser: Callable[[str], object],
    cancel: threading.Event | None = None,
    timeout_s: float = SIGN_IN_TIMEOUT_S,
) -> Credentials:
    """Run the browser flow and return fresh credentials (with a refresh token)."""
    server = _Receiver(("127.0.0.1", 0), _Handler)
    server.timeout = 0.25
    try:
        redirect_uri = f"http://127.0.0.1:{server.server_address[1]}/"
        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(24)
        open_browser(auth_url(client, redirect_uri, state, challenge))
        deadline = time.monotonic() + timeout_s
        while server.result is None:
            if cancel is not None and cancel.is_set():
                raise AuthCancelled()
            if time.monotonic() > deadline:
                raise YouTubeAuthError("Sign-in timed out. Try again.")
            server.handle_request()
        result = server.result
    finally:
        server.server_close()
    if "error" in result:
        if result["error"] == "access_denied":
            raise AuthCancelled()
        raise YouTubeAuthError(f"Google refused the sign-in: {result['error']}")
    if result.get("state") != state:
        raise YouTubeAuthError("Sign-in response did not match the request (state mismatch).")
    return exchange_code(client, result["code"], verifier, redirect_uri)


def _post_form(url: str, fields: dict[str, str], timeout: float = 30) -> dict[str, Any]:
    data = urllib.parse.urlencode(fields).encode()
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read() or b"{}")
        except ValueError:
            payload = {}
        error = payload.get("error", "") if isinstance(payload, dict) else ""
        if error in ("invalid_grant", "unauthorized_client", "invalid_client"):
            raise YouTubeAuthError(
                "Google no longer accepts the saved sign-in (expired or revoked). Reconnect the account.",
                exc.code, error,
            ) from exc
        raise YouTubeError(f"Google sign-in failed: HTTP {exc.code} {error}".strip(), exc.code, error) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise YouTubeError(f"Cannot reach Google: {exc}") from exc


def _credentials(payload: dict[str, Any], refresh_token: str = "") -> Credentials:
    if "access_token" not in payload:
        raise YouTubeAuthError("Google did not return an access token.")
    refresh = payload.get("refresh_token") or refresh_token
    if not refresh:
        raise YouTubeAuthError("Google did not return a refresh token. Remove the app's access and reconnect.")
    scopes = tuple(str(payload.get("scope", " ".join(SCOPES))).split())
    return Credentials(
        access_token=payload["access_token"],
        refresh_token=refresh,
        expires_at=time.time() + float(payload.get("expires_in", 3600)),
        scopes=scopes,
    )


def exchange_code(client: ClientConfig, code: str, verifier: str, redirect_uri: str) -> Credentials:
    payload = _post_form(TOKEN_URL, {
        "client_id": client.client_id,
        "client_secret": client.client_secret,
        "code": code,
        "code_verifier": verifier,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    })
    creds = _credentials(payload)
    if not set(REQUIRED_SCOPES) <= set(creds.scopes):
        raise YouTubeAuthError("Not all permissions were granted. Reconnect and allow uploading videos.")
    return creds


def refresh(client: ClientConfig, refresh_token: str) -> Credentials:
    payload = _post_form(TOKEN_URL, {
        "client_id": client.client_id,
        "client_secret": client.client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    })
    return _credentials(payload, refresh_token)


def revoke(token: str) -> None:
    """Best effort: tell Google to forget the grant."""
    try:
        _post_form(REVOKE_URL, {"token": token}, timeout=15)
    except YouTubeError:
        pass
