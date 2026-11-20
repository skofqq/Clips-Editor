"""Instagram Reels through the Instagram API with Instagram Login.

Connecting uses an access token generated in the Meta app dashboard
(Instagram → API setup with Instagram login → Generate token). It lasts 60
days and is refreshed automatically while the app is used. Posting uses the
resumable upload: the video goes straight from this PC to Instagram, no
public link is needed.
"""

from __future__ import annotations

import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from core.social.errors import PostCancelled, SocialAuthError, SocialError
from core.social.web import call

GRAPH_URL = "https://graph.instagram.com"
VERSION = "v21.0"
UPLOAD_URL = "https://rupload.facebook.com/ig-api-upload"
CAPTION_MAX = 2200
REFRESH_AFTER_S = 7 * 86400  # refresh a 60-day token once it is a week old
TOKEN_LIFE_S = 60 * 86400


@dataclass
class InstagramAccount:
    user_id: str
    username: str
    token: str
    issued_at: float  # when the token was made or last refreshed

    @property
    def expires_at(self) -> float:
        return self.issued_at + TOKEN_LIFE_S

    def to_dict(self) -> dict:
        return {"user_id": self.user_id, "username": self.username, "token": self.token,
                "issued_at": self.issued_at}

    @classmethod
    def from_dict(cls, data: dict) -> "InstagramAccount":
        return cls(str(data["user_id"]), str(data.get("username", "")), str(data["token"]),
                   float(data.get("issued_at", 0)))


@dataclass(frozen=True)
class ReelResult:
    media_id: str
    url: str


def _error(status: int, payload: dict) -> SocialError:
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return SocialError(f"Instagram error {status}: {error or payload}", status)
    code = str(error.get("code", ""))
    message = str(error.get("error_user_msg") or error.get("message") or "")
    if code == "190" or error.get("type") == "OAuthException" and status in (400, 401):
        return SocialAuthError("The Instagram token is invalid or expired. Generate a new token in the Meta "
                               "dashboard and connect again.", status, code)
    if code in ("10", "200"):
        return SocialAuthError("The token has no permission to publish. Generate it with "
                               "instagram_business_content_publish allowed.", status, code)
    if code in ("4", "9", "32", "613") or "limit" in message.lower():
        return SocialError(f"Instagram limit reached: {message}", status, code)
    return SocialError(f"Instagram error {code}: {message}", status, code)


def _graph(path: str, **query) -> str:
    return f"{GRAPH_URL}/{VERSION}/{path}" + (f"?{urllib.parse.urlencode(query)}" if query else "")


def connect(token: str) -> InstagramAccount:
    """Check a token and read the account it belongs to (a professional account)."""
    token = token.strip()
    if not token:
        raise SocialAuthError("Paste the access token first.")
    me = call("GET", _graph("me", fields="user_id,username,account_type", access_token=token), parse_error=_error)
    user_id = str(me.get("user_id") or me.get("id") or "")
    if not user_id:
        raise SocialError("Instagram did not say which account the token belongs to.")
    if str(me.get("account_type", "")).upper() == "PERSONAL":
        raise SocialError("Reels can be posted only from a professional account (Business or Creator). "
                          "Switch it in the Instagram app: Settings → Account type and tools.")
    return InstagramAccount(user_id, str(me.get("username", "")), token, time.time())


def refresh(account: InstagramAccount, now: float | None = None) -> InstagramAccount:
    """A fresh 60-day token when the current one is a week old (else unchanged)."""
    now = now if now is not None else time.time()
    if now - account.issued_at < REFRESH_AFTER_S:
        return account
    if now > account.expires_at:
        raise SocialAuthError("The Instagram token expired. Generate a new one and connect again.")
    payload = call("GET", f"{GRAPH_URL}/refresh_access_token?" + urllib.parse.urlencode(
        {"grant_type": "ig_refresh_token", "access_token": account.token}), parse_error=_error)
    token = str(payload.get("access_token") or "")
    if not token:
        return account
    return InstagramAccount(account.user_id, account.username, token, now)


def post_reel(account: InstagramAccount, path: Path, caption: str, share_to_feed: bool = True,
              cover_ms: int | None = None, progress=None, cancel: threading.Event | None = None,
              poll_s: float = 5.0, max_wait_s: float = 900) -> ReelResult:
    path = Path(path)
    size = path.stat().st_size
    form = {"media_type": "REELS", "upload_type": "resumable", "caption": caption[:CAPTION_MAX],
            "share_to_feed": "true" if share_to_feed else "false", "access_token": account.token}
    if cover_ms is not None:
        form["thumb_offset"] = str(max(0, int(cover_ms)))
    created = call("POST", _graph(f"{account.user_id}/media"), form=form, parse_error=_error)
    container = str(created.get("id", ""))
    if not container:
        raise SocialError("Instagram did not create the Reel.")
    upload_uri = str(created.get("uri") or f"{UPLOAD_URL}/{VERSION}/{container}")
    if progress is not None:
        progress(0.1, "Instagram: uploading…")
    if cancel is not None and cancel.is_set():
        raise PostCancelled()
    reply = call("POST", upload_uri, data=path.read_bytes(), parse_error=_error, timeout=600, headers={
        "Authorization": f"OAuth {account.token}", "offset": "0", "file_size": str(size),
        "Content-Type": "application/octet-stream"})
    if reply.get("success") is False:
        raise SocialError(f"Instagram did not accept the file: {reply.get('message', reply)}")
    deadline = time.monotonic() + max_wait_s
    while True:
        if cancel is not None and cancel.is_set():
            raise PostCancelled()
        state = call("GET", _graph(container, fields="status_code,status", access_token=account.token),
                     parse_error=_error)
        code = str(state.get("status_code", ""))
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise SocialError(f"Instagram could not process the video: {state.get('status') or code}")
        if time.monotonic() > deadline:
            raise SocialError("Instagram is still processing the video. Try publishing again later.")
        if progress is not None:
            progress(0.6, "Instagram: processing…")
        time.sleep(poll_s)
    if progress is not None:
        progress(0.9, "Instagram: publishing…")
    published = call("POST", _graph(f"{account.user_id}/media_publish"), parse_error=_error,
                     form={"creation_id": container, "access_token": account.token})
    media_id = str(published.get("id", ""))
    url = ""
    try:
        url = str(call("GET", _graph(media_id, fields="permalink", access_token=account.token),
                       parse_error=_error).get("permalink", ""))
    except SocialError:
        pass
    return ReelResult(media_id, url)
