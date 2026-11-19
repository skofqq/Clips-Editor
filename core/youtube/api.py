"""YouTube Data API v3: channels and resumable video uploads.

Uses only the standard library. Uploads follow Google's resumable upload
protocol: one request creates an upload session, the file is sent in
chunks (each a multiple of 256 KiB), and after a network error the client
asks the server how much it received and continues from there.
"""

from __future__ import annotations

import http.client
import json
import logging
import mimetypes
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from core.youtube.errors import UploadCancelled, YouTubeAuthError, YouTubeError, YouTubeQuotaError
from core.youtube.metadata import VideoMetadata

log = logging.getLogger(__name__)

API_URL = "https://www.googleapis.com/youtube/v3"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
CAPTIONS_URL = "https://www.googleapis.com/upload/youtube/v3/captions"
CHUNK = 8 * 1024 * 1024  # bytes per request; a multiple of 256 KiB
PIECE = 256 * 1024  # bytes written between progress / cancel checks
MAX_RETRIES = 8
RETRY_STATUS = {500, 502, 503, 504}

TokenProvider = Callable[[bool], str]  # (force_refresh) -> access token
Progress = Callable[[float, str], None]


@dataclass(frozen=True)
class Channel:
    id: str
    title: str
    custom_url: str = ""
    thumbnail_url: str = ""

    @property
    def url(self) -> str:
        if self.custom_url.startswith("@"):
            return f"https://www.youtube.com/{self.custom_url}"
        return f"https://www.youtube.com/channel/{self.id}"

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "title": self.title, "custom_url": self.custom_url,
                "thumbnail_url": self.thumbnail_url}

    @classmethod
    def from_dict(cls, data: dict) -> "Channel":
        return cls(str(data["id"]), str(data.get("title", "")), str(data.get("custom_url", "")),
                   str(data.get("thumbnail_url", "")))


@dataclass(frozen=True)
class UploadResult:
    video_id: str
    privacy: str
    publish_at: str
    upload_status: str
    captions: str = ""  # "" = not asked, "added", or why they could not be added

    @property
    def url(self) -> str:
        return f"https://youtube.com/shorts/{self.video_id}"

    @property
    def watch_url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"


def _error_from(status: int, raw: bytes) -> YouTubeError:
    try:
        payload = json.loads(raw or b"{}")
    except ValueError:
        payload = {}
    error = payload.get("error") if isinstance(payload, dict) else None
    message, reason = "", ""
    if isinstance(error, dict):
        message = str(error.get("message", ""))
        errors = error.get("errors") or []
        if errors and isinstance(errors[0], dict):
            reason = str(errors[0].get("reason", ""))
    elif isinstance(error, str):
        reason = error
    if reason in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"):
        return YouTubeQuotaError(
            "The daily YouTube API quota is used up (an upload costs a large part of it). "
            "Try again after midnight Pacific Time.", status, reason)
    if reason == "uploadLimitExceeded":
        return YouTubeQuotaError("This channel reached YouTube's upload limit for now. Try again later.",
                                 status, reason)
    if reason == "youtubeSignupRequired":
        return YouTubeError("This Google account has no YouTube channel. Create one, then reconnect.",
                            status, reason)
    if status == 401:
        return YouTubeAuthError("YouTube sign-in expired. Reconnect the account.", status, reason)
    if status == 403 and (reason in ("insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT")
                          or "insufficient authentication scopes" in message.lower()):
        return YouTubeAuthError("This account was connected without permission to add captions. "
                                "Open Accounts…, reconnect the channel and allow managing your YouTube videos.",
                                status, "insufficientPermissions")
    text = message or reason or http.client.responses.get(status, "")
    return YouTubeError(f"YouTube error {status}: {text}", status, reason)


class YouTubeClient:
    def __init__(self, token: TokenProvider, timeout: float = 60.0) -> None:
        self._token = token
        self.timeout = timeout

    # ------------------------------------------------------------ JSON calls
    def _call(self, method: str, url: str, body: Any = None, headers: dict | None = None,
              want_headers: bool = False):
        for attempt in (0, 1):
            data = json.dumps(body).encode() if body is not None else None
            request = urllib.request.Request(url, data=data, method=method)
            request.add_header("Authorization", f"Bearer {self._token(attempt == 1)}")
            if data is not None:
                request.add_header("Content-Type", "application/json; charset=UTF-8")
            for key, value in (headers or {}).items():
                request.add_header(key, value)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw = response.read()
                    payload = json.loads(raw) if raw else {}
                    return (payload, dict(response.headers)) if want_headers else payload
            except urllib.error.HTTPError as exc:
                raw = exc.read()
                if exc.code == 401 and attempt == 0:
                    continue  # refresh the token once
                raise _error_from(exc.code, raw) from exc
            except (urllib.error.URLError, OSError) as exc:
                raise YouTubeError(f"Cannot reach YouTube: {exc}") from exc
        raise YouTubeAuthError("YouTube sign-in expired. Reconnect the account.", 401)

    def my_channels(self) -> list[Channel]:
        payload = self._call("GET", f"{API_URL}/channels?part=snippet&mine=true&maxResults=50")
        channels = []
        for item in payload.get("items") or []:
            snippet = item.get("snippet") or {}
            thumbs = snippet.get("thumbnails") or {}
            thumb = (thumbs.get("default") or thumbs.get("medium") or {}).get("url", "")
            channels.append(Channel(item["id"], snippet.get("title", ""), snippet.get("customUrl", ""), thumb))
        return channels

    # ------------------------------------------------------------ upload
    def start_upload(self, path: Path, metadata: VideoMetadata) -> str:
        """Create a resumable upload session; returns its URL."""
        size = path.stat().st_size
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix.lower() == ".mkv":
            mime = "video/x-matroska"
        url = f"{UPLOAD_URL}?uploadType=resumable&part=snippet,status"
        _, headers = self._call(
            "POST", url, metadata.body(),
            {"X-Upload-Content-Length": str(size), "X-Upload-Content-Type": mime},
            want_headers=True,
        )
        location = next((v for k, v in headers.items() if k.lower() == "location"), "")
        if not location:
            raise YouTubeError("YouTube did not start an upload session.")
        return location

    def upload(
        self,
        path: str | Path,
        metadata: VideoMetadata,
        progress: Progress | None = None,
        cancel: threading.Event | None = None,
        session_url: str | None = None,
    ) -> UploadResult:
        path = Path(path)
        progress = progress or (lambda f, m="": None)
        total = path.stat().st_size
        if total == 0:
            raise YouTubeError("The video file is empty.")
        progress(0.0, "Starting upload…")
        session = session_url or self.start_upload(path, metadata)
        offset, retries = 0, 0
        started, sent_this_run = time.monotonic(), 0
        while True:
            if cancel is not None and cancel.is_set():
                raise UploadCancelled()
            try:
                if retries:
                    status, headers, raw = self._put(session, None, f"bytes */{total}", None, None, total, 0)
                else:
                    end = min(offset + CHUNK, total) - 1

                    def report(done: int, base=offset) -> None:
                        nonlocal sent_this_run
                        position = base + done
                        elapsed = max(1e-6, time.monotonic() - started)
                        speed = (sent_this_run + done) / elapsed
                        eta = (total - position) / speed if speed > 0 else None
                        progress(min(0.999, position / total), _message(position, total, speed, eta))

                    status, headers, raw = self._put(
                        session, path, f"bytes {offset}-{end}/{total}", report, cancel, total, offset,
                        length=end - offset + 1,
                    )
                    sent_this_run += end - offset + 1
            except UploadCancelled:
                raise
            except (OSError, http.client.HTTPException, socket.timeout, ssl.SSLError) as exc:
                retries = self._backoff(retries, f"network error: {exc}", cancel)
                continue
            if status in (200, 201):
                video = json.loads(raw or b"{}")
                st = video.get("status") or {}
                progress(1.0, "Uploaded")
                return UploadResult(
                    video_id=str(video.get("id", "")),
                    privacy=str(st.get("privacyStatus", "")),
                    publish_at=str(st.get("publishAt", "")),
                    upload_status=str(st.get("uploadStatus", "")),
                )
            if status == 308:  # "Resume Incomplete": continue after the last byte received
                received = _range_end(headers)
                offset = received + 1 if received is not None else 0
                retries = 0
                continue
            if status == 401:
                self._token(True)  # refresh, then ask where to continue
                retries = max(retries, 1)
                continue
            if status in RETRY_STATUS:
                retries = self._backoff(retries, f"HTTP {status}", cancel)
                continue
            if status == 404:
                raise YouTubeError("The upload session expired. Start the upload again.", 404)
            raise _error_from(status, raw)

    # ------------------------------------------------------------ captions
    def upload_captions(self, video_id: str, text: str, language: str, name: str = "",
                        filename: str = "captions.srt") -> str:
        """Add a caption track (SubRip text) to ``video_id``; returns its id."""
        boundary = f"tss{int(time.time() * 1000)}"
        snippet = {"snippet": {"videoId": video_id, "language": language or "ru", "name": name, "isDraft": False}}
        body = (
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(snippet)}\r\n"
            f"--{boundary}\r\nContent-Type: application/octet-stream\r\n"
            f"Content-Disposition: attachment; filename=\"{filename}\"\r\n\r\n"
        ).encode() + text.encode("utf-8") + f"\r\n--{boundary}--\r\n".encode()
        url = f"{CAPTIONS_URL}?uploadType=multipart&part=snippet"
        for attempt in (0, 1):
            request = urllib.request.Request(url, data=body, method="POST")
            request.add_header("Authorization", f"Bearer {self._token(attempt == 1)}")
            request.add_header("Content-Type", f"multipart/related; boundary={boundary}")
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw = response.read()
                    return str((json.loads(raw) if raw else {}).get("id", ""))
            except urllib.error.HTTPError as exc:
                raw = exc.read()
                if exc.code == 401 and attempt == 0:
                    continue
                raise _error_from(exc.code, raw) from exc
            except (urllib.error.URLError, OSError) as exc:
                raise YouTubeError(f"Cannot reach YouTube: {exc}") from exc
        raise YouTubeAuthError("YouTube sign-in expired. Reconnect the account.", 401)

    def _backoff(self, retries: int, why: str, cancel: threading.Event | None) -> int:
        retries += 1
        if retries > MAX_RETRIES:
            raise YouTubeError(f"Upload failed after {MAX_RETRIES} retries ({why}).")
        delay = min(64, 2 ** (retries - 1))
        log.warning("Upload interrupted (%s); retry %d in %d s", why, retries, delay)
        end = time.monotonic() + delay
        while time.monotonic() < end:
            if cancel is not None and cancel.is_set():
                raise UploadCancelled()
            time.sleep(0.1)
        return retries

    def _put(self, url: str, path: Path | None, content_range: str, report, cancel, total: int,
             offset: int, length: int = 0):
        parts = urllib.parse.urlsplit(url)
        connection_cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        conn = connection_cls(parts.netloc, timeout=self.timeout)
        target = parts.path + (f"?{parts.query}" if parts.query else "")
        try:
            conn.putrequest("PUT", target)
            conn.putheader("Authorization", f"Bearer {self._token(False)}")
            conn.putheader("Content-Length", str(length))
            conn.putheader("Content-Range", content_range)
            conn.endheaders()
            if path is not None and length:
                with path.open("rb") as f:
                    f.seek(offset)
                    done = 0
                    while done < length:
                        if cancel is not None and cancel.is_set():
                            raise UploadCancelled()
                        block = f.read(min(PIECE, length - done))
                        if not block:
                            raise OSError("The file got shorter during upload.")
                        conn.send(block)
                        done += len(block)
                        if report is not None:
                            report(done)
            response = conn.getresponse()
            return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
        finally:
            conn.close()


def _range_end(headers: dict[str, str]) -> int | None:
    value = headers.get("range", "")
    if "-" not in value:
        return None
    try:
        return int(value.rsplit("-", 1)[1])
    except ValueError:
        return None


def _message(position: int, total: int, speed: float, eta: float | None) -> str:
    mb = 1024 * 1024
    text = f"{position / total * 100:.0f}% · {position / mb:.1f} / {total / mb:.1f} MB · {speed / mb:.1f} MB/s"
    if eta is not None:
        m, s = divmod(int(round(eta)), 60)
        text += f" · {m}:{s:02d} left"
    return text
