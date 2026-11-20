"""TikTok: sign-in (Login Kit for Desktop, PKCE) and the Content Posting API.

Two ways to post:
- direct: the video is published on the profile. Until TikTok audits the app,
  only "Only me" (SELF_ONLY) is allowed, and the API says so.
- inbox: the video lands in the TikTok app's inbox as a draft; you finish
  the post (caption, sound, cover) in the app.
"""

from __future__ import annotations

import hashlib
import math
import secrets
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from core.social.errors import PostCancelled, SocialAuthError, SocialError
from core.social.web import call
from core.youtube import oauth as google_oauth

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
API_URL = "https://open.tiktokapis.com/v2"
SCOPES = ("user.info.basic", "video.publish", "video.upload")
REDIRECT_PORT = 8765
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback/"
DIRECT, INBOX = "direct", "inbox"
MIN_CHUNK, MAX_CHUNK = 5 * 1024 * 1024, 64 * 1024 * 1024
CHUNK = 10 * 1024 * 1024
PRIVACY_TEXT = {
    "PUBLIC_TO_EVERYONE": "Everyone",
    "MUTUAL_FOLLOW_FRIENDS": "Friends",
    "FOLLOWER_OF_CREATOR": "Followers",
    "SELF_ONLY": "Only me",
}
CAPTION_MAX = 2200


@dataclass(frozen=True)
class TikTokApp:
    client_key: str
    client_secret: str


@dataclass
class TikTokToken:
    access_token: str
    refresh_token: str
    expires_at: float
    refresh_expires_at: float
    open_id: str
    scopes: tuple[str, ...] = SCOPES

    @property
    def expired(self) -> bool:
        return time.time() > self.expires_at - 120

    def to_dict(self) -> dict:
        return {"access_token": self.access_token, "refresh_token": self.refresh_token,
                "expires_at": self.expires_at, "refresh_expires_at": self.refresh_expires_at,
                "open_id": self.open_id, "scopes": list(self.scopes)}

    @classmethod
    def from_dict(cls, data: dict) -> "TikTokToken":
        return cls(str(data["access_token"]), str(data.get("refresh_token", "")),
                   float(data.get("expires_at", 0)), float(data.get("refresh_expires_at", 0)),
                   str(data.get("open_id", "")), tuple(data.get("scopes") or SCOPES))


@dataclass(frozen=True)
class CreatorInfo:
    nickname: str
    username: str
    privacy_options: tuple[str, ...]
    max_duration_s: int
    comments_off: bool = False


@dataclass(frozen=True)
class TikTokResult:
    publish_id: str
    status: str  # PUBLISH_COMPLETE, SEND_TO_USER_INBOX, …
    url: str = ""
    message: str = ""


@dataclass
class TikTokPost:
    caption: str = ""
    mode: str = DIRECT
    privacy: str = "SELF_ONLY"
    cover_ms: int = 0
    allow_comments: bool = True
    allow_duet: bool = True
    allow_stitch: bool = True
    extra: dict = field(default_factory=dict)


# ------------------------------------------------------------------ errors
def _error(status: int, payload: dict) -> SocialError:
    error = payload.get("error")
    if isinstance(error, dict):
        code, message = str(error.get("code", "")), str(error.get("message", ""))
    else:
        code, message = str(error or ""), str(payload.get("error_description", ""))
    return tiktok_error(code, message, status)


def tiktok_error(code: str, message: str, status: int = 0) -> SocialError:
    known = {
        "access_token_invalid": "TikTok sign-in expired. Connect TikTok again.",
        "scope_not_authorized": "TikTok did not allow posting for this app. Connect again and allow all "
                                "permissions; the app needs the Content Posting API product.",
        "invalid_grant": "TikTok no longer accepts the saved sign-in. Connect TikTok again.",
        "spam_risk_too_many_posts": "TikTok's daily posting limit for this account is reached. Try tomorrow.",
        "spam_risk_user_banned_from_posting": "TikTok does not let this account post right now.",
        "unaudited_client_can_only_post_to_private_accounts":
            "TikTok has not audited this app yet: it can only post with “Only me” visibility, and the account "
            "must be private. Use “Only me”, or “Send to TikTok drafts”.",
        "privacy_level_option_mismatch": "This visibility is not available for the account. Pick another one.",
        "url_ownership_unverified": "TikTok refused the upload source.",
    }
    text = known.get(code) or f"TikTok error {status or ''} {code}: {message}".replace("  ", " ").strip()
    cls = SocialAuthError if code in ("access_token_invalid", "invalid_grant", "scope_not_authorized") else SocialError
    return cls(text, status, code)


def _check(payload: dict) -> dict:
    """TikTok answers 200 with error.code "ok" on success."""
    error = payload.get("error") or {}
    if isinstance(error, dict) and error.get("code") not in (None, "", "ok"):
        raise tiktok_error(str(error.get("code")), str(error.get("message", "")))
    return payload.get("data") or {}


# ------------------------------------------------------------------ sign-in
def pkce_pair() -> tuple[str, str]:
    """TikTok's desktop PKCE: the challenge is the hex SHA-256 of the verifier."""
    verifier = secrets.token_urlsafe(48)[:64]
    return verifier, hashlib.sha256(verifier.encode()).hexdigest()


def auth_url(app: TikTokApp, state: str, challenge: str, redirect_uri: str = REDIRECT_URI) -> str:
    query = {"client_key": app.client_key, "scope": ",".join(SCOPES), "response_type": "code",
             "redirect_uri": redirect_uri, "state": state, "code_challenge": challenge,
             "code_challenge_method": "S256"}
    return f"{AUTH_URL}?{urllib.parse.urlencode(query)}"


def _token(payload: dict, old_refresh: str = "") -> TikTokToken:
    if "access_token" not in payload:
        raise _error(400, payload)
    now = time.time()
    return TikTokToken(
        access_token=str(payload["access_token"]),
        refresh_token=str(payload.get("refresh_token") or old_refresh),
        expires_at=now + float(payload.get("expires_in", 86400)),
        refresh_expires_at=now + float(payload.get("refresh_expires_in", 365 * 86400)),
        open_id=str(payload.get("open_id", "")),
        scopes=tuple(str(payload.get("scope", ",".join(SCOPES))).split(",")),
    )


def sign_in(app: TikTokApp, open_browser: Callable[[str], object], cancel: threading.Event | None = None,
            timeout_s: float = 300, port: int = REDIRECT_PORT) -> TikTokToken:
    """Browser sign-in; TikTok sends the browser back to localhost:<port>/callback/."""
    try:
        server = google_oauth._Receiver(("127.0.0.1", port), google_oauth._Handler)
    except OSError as exc:
        raise SocialError(f"Port {port} is busy: close the program using it and try again ({exc}).") from exc
    server.timeout = 0.25
    redirect_uri = f"http://localhost:{server.server_address[1]}/callback/"
    try:
        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(24)
        open_browser(auth_url(app, state, challenge, redirect_uri))
        deadline = time.monotonic() + timeout_s
        while server.result is None:
            if cancel is not None and cancel.is_set():
                raise PostCancelled()
            if time.monotonic() > deadline:
                raise SocialAuthError("TikTok sign-in timed out. Try again.")
            server.handle_request()
        result = server.result
    finally:
        server.server_close()
    if "error" in result:
        raise SocialAuthError(f"TikTok refused the sign-in: {result.get('error_description') or result['error']}")
    if result.get("state") != state:
        raise SocialAuthError("TikTok sign-in response did not match the request.")
    payload = call("POST", f"{API_URL}/oauth/token/", parse_error=_error, form={
        "client_key": app.client_key, "client_secret": app.client_secret, "code": result["code"],
        "grant_type": "authorization_code", "redirect_uri": redirect_uri, "code_verifier": verifier})
    return _token(payload)


def refresh(app: TikTokApp, token: TikTokToken) -> TikTokToken:
    if token.refresh_expires_at and time.time() > token.refresh_expires_at:
        raise SocialAuthError("TikTok sign-in expired (a year passed). Connect TikTok again.")
    payload = call("POST", f"{API_URL}/oauth/token/", parse_error=_error, form={
        "client_key": app.client_key, "client_secret": app.client_secret,
        "grant_type": "refresh_token", "refresh_token": token.refresh_token})
    fresh = _token(payload, token.refresh_token)
    fresh.open_id = fresh.open_id or token.open_id
    return fresh


def revoke(app: TikTokApp, token: TikTokToken) -> None:
    try:
        call("POST", f"{API_URL}/oauth/revoke/", parse_error=_error, form={
            "client_key": app.client_key, "client_secret": app.client_secret, "token": token.access_token})
    except SocialError:
        pass


# ------------------------------------------------------------------ API
def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def user_name(access_token: str) -> str:
    payload = call("GET", f"{API_URL}/user/info/?fields=open_id,display_name", headers=_auth(access_token),
                   parse_error=_error)
    return str((_check(payload).get("user") or {}).get("display_name", ""))


def creator_info(access_token: str) -> CreatorInfo:
    payload = call("POST", f"{API_URL}/post/publish/creator_info/query/", headers=_auth(access_token),
                   json_body={}, parse_error=_error)
    data = _check(payload)
    return CreatorInfo(
        nickname=str(data.get("creator_nickname", "")),
        username=str(data.get("creator_username", "")),
        privacy_options=tuple(data.get("privacy_level_options") or ("SELF_ONLY",)),
        max_duration_s=int(data.get("max_video_post_duration_sec") or 600),
        comments_off=bool(data.get("comment_disabled", False)),
    )


def chunks(size: int) -> tuple[int, int]:
    """(chunk size, chunk count) following TikTok's rules: one chunk up to 64 MB,
    else 10 MB chunks with the remainder in the last one."""
    if size <= MAX_CHUNK:
        return size, 1
    return CHUNK, size // CHUNK


def post_video(access_token: str, path: Path, post: TikTokPost, progress=None,
               cancel: threading.Event | None = None, poll_s: float = 3.0, max_wait_s: float = 600,
               username: str = "") -> TikTokResult:
    path = Path(path)
    size = path.stat().st_size
    chunk_size, count = chunks(size)
    source = {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk_size, "total_chunk_count": count}
    if post.mode == INBOX:
        url, body = f"{API_URL}/post/publish/inbox/video/init/", {"source_info": source}
    else:
        url = f"{API_URL}/post/publish/video/init/"
        body = {"source_info": source, "post_info": {
            "title": post.caption[:CAPTION_MAX],
            "privacy_level": post.privacy,
            "disable_comment": not post.allow_comments,
            "disable_duet": not post.allow_duet,
            "disable_stitch": not post.allow_stitch,
            "video_cover_timestamp_ms": max(0, int(post.cover_ms)),
        }}
    data = _check(call("POST", url, headers=_auth(access_token), json_body=body, parse_error=_error))
    publish_id, upload_url = str(data.get("publish_id", "")), str(data.get("upload_url", ""))
    if not publish_id or not upload_url:
        raise SocialError("TikTok did not return an upload address.")
    with path.open("rb") as handle:
        for index in range(count):
            if cancel is not None and cancel.is_set():
                raise PostCancelled()
            start = index * chunk_size
            length = size - start if index == count - 1 else chunk_size
            piece = handle.read(length)
            call("PUT", upload_url, data=piece, parse_error=_error, timeout=300, headers={
                "Content-Type": "video/mp4", "Content-Length": str(length),
                "Content-Range": f"bytes {start}-{start + length - 1}/{size}"})
            if progress is not None:
                progress(0.9 * (index + 1) / count, f"TikTok: uploading {math.floor(100 * (index + 1) / count)}%")
    # Wait until TikTok has processed it.
    deadline = time.monotonic() + max_wait_s
    while True:
        if cancel is not None and cancel.is_set():
            raise PostCancelled()
        data = _check(call("POST", f"{API_URL}/post/publish/status/fetch/", headers=_auth(access_token),
                           json_body={"publish_id": publish_id}, parse_error=_error))
        status = str(data.get("status", ""))
        if status == "FAILED":
            raise tiktok_error(str(data.get("fail_reason", "failed")), "TikTok could not publish the video")
        if status in ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"):
            ids = data.get("publicaly_available_post_id") or data.get("publicly_available_post_id") or []
            url = f"https://www.tiktok.com/@{username}/video/{ids[0]}" if ids and username else ""
            message = ("Sent to your TikTok inbox: open TikTok to finish the post." if status == "SEND_TO_USER_INBOX"
                       else "Posted on TikTok." + ("" if ids else " It is not public (Only me), or TikTok is "
                                                                 "still reviewing it."))
            return TikTokResult(publish_id, status, url, message)
        if time.monotonic() > deadline:
            return TikTokResult(publish_id, status, "", "Uploaded; TikTok is still processing it. "
                                                        "Check the app in a few minutes.")
        if progress is not None:
            progress(0.95, "TikTok: processing…")
        time.sleep(poll_s)
