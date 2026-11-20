"""Phase 7: OAuth, secure token storage, resumable upload, metadata checks (fake Google)."""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from core.youtube import api as api_mod
from core.youtube import oauth
from core.youtube.api import Channel, YouTubeClient
from core.youtube.errors import (
    AuthCancelled,
    UploadCancelled,
    YouTubeAuthError,
    YouTubeError,
    YouTubeQuotaError,
)
from core.youtube.history import UploadHistory, UploadRecord
from core.youtube.metadata import PRIVATE, PUBLIC, VideoMetadata, parse_tags, tags_length
from core.youtube.oauth import ClientConfig
from core.youtube.service import YouTubeService
from core.youtube.storage import MemoryStore, default_store


class FakeGoogle:
    """Just enough of accounts.google.com + the YouTube API for the client."""

    def __init__(self) -> None:
        self.uploaded = bytearray()
        self.total = 0
        self.metadata: dict | None = None
        self.valid_tokens = {"access-1"}
        self.refresh_count = 0
        self.drop_next_chunk = False  # simulate a network failure mid-upload
        self.fail_status: int | None = None  # one-off HTTP error on the next PUT
        self.quota = False
        self.revoked: list[str] = []
        self.puts = 0
        self.captions: list[str] = []
        self.captions_scope_missing = False
        self.thumbnails: list = []
        self.thumbnail_forbidden = False
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, payload=None, headers=None):
                body = json.dumps(payload).encode() if payload is not None else b""
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _authorized(self):
                token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                return token in fake.valid_tokens

            def _body(self):
                length = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(length) if length else b""

            def do_POST(self):
                url = urllib.parse.urlparse(self.path)
                if url.path == "/token":
                    form = dict(urllib.parse.parse_qsl(self._body().decode()))
                    if form["grant_type"] == "authorization_code":
                        assert form["code"] == "good-code" and len(form["code_verifier"]) >= 43
                        return self._send(200, {"access_token": "access-1", "refresh_token": "refresh-1",
                                                "expires_in": 3600, "scope": " ".join(oauth.SCOPES)})
                    if form.get("refresh_token") != "refresh-1":
                        return self._send(400, {"error": "invalid_grant"})
                    fake.refresh_count += 1
                    token = f"access-r{fake.refresh_count}"
                    fake.valid_tokens.add(token)
                    return self._send(200, {"access_token": token, "expires_in": 3600,
                                            "scope": " ".join(oauth.SCOPES)})
                if url.path == "/revoke":
                    fake.revoked.append(dict(urllib.parse.parse_qsl(self._body().decode()))["token"])
                    return self._send(200, {})
                if url.path == "/thumbnails":
                    body = self._body()
                    if fake.thumbnail_forbidden:
                        return self._send(403, {"error": {"code": 403, "message": "The authenticated user doesn't "
                                                          "have permissions to upload and set custom video "
                                                          "thumbnails.", "errors": [{"reason": "forbidden"}]}})
                    query = dict(urllib.parse.parse_qsl(url.query))
                    fake.thumbnails.append((query["videoId"], self.headers["Content-Type"], body))
                    return self._send(200, {"kind": "youtube#thumbnailSetResponse"})
                if url.path == "/captions":
                    if not self._authorized():
                        return self._send(401, {"error": {"code": 401}})
                    if fake.captions_scope_missing:
                        self._body()
                        return self._send(403, {"error": {"code": 403, "message": "Request had insufficient "
                                                          "authentication scopes.",
                                                          "errors": [{"reason": "insufficientPermissions"}]}})
                    assert "multipart/related" in self.headers["Content-Type"]
                    fake.captions.append(self._body().decode("utf-8"))
                    return self._send(200, {"id": "cap1"})
                if url.path == "/upload":
                    if not self._authorized():
                        return self._send(401, {"error": {"code": 401, "message": "Invalid Credentials"}})
                    if fake.quota:
                        return self._send(403, {"error": {"code": 403, "message": "quota",
                                                          "errors": [{"reason": "quotaExceeded"}]}})
                    fake.metadata = json.loads(self._body())
                    fake.total = int(self.headers["X-Upload-Content-Length"])
                    host = self.headers["Host"]
                    return self._send(200, {}, {"Location": f"http://{host}/session/1?upload_id=x"})
                self._send(404, {})

            def do_GET(self):
                url = urllib.parse.urlparse(self.path)
                if url.path == "/youtube/v3/channels":
                    if not self._authorized():
                        return self._send(401, {"error": {"code": 401, "message": "Invalid Credentials"}})
                    return self._send(200, {"items": [{"id": "UC123", "snippet": {
                        "title": "Skof Clips", "customUrl": "@skofclips",
                        "thumbnails": {"default": {"url": "http://example/avatar.jpg"}}}}]})
                self._send(404, {})

            def do_PUT(self):
                fake.puts += 1
                if not self._authorized():
                    self._body()
                    return self._send(401, {"error": {"code": 401}})
                rng = self.headers["Content-Range"]
                body = self._body()
                if fake.fail_status:
                    status, fake.fail_status = fake.fail_status, None
                    return self._send(status, {"error": {"code": status, "message": "backend"}})
                if rng.startswith("bytes */"):
                    pass  # status query
                else:
                    start = int(rng.split()[1].split("-")[0])
                    if fake.drop_next_chunk:
                        fake.drop_next_chunk = False
                        # Keep half of it, then cut the connection without answering.
                        fake.uploaded[start:] = body[: len(body) // 2]
                        self.close_connection = True
                        self.connection.shutdown(2)
                        return
                    fake.uploaded[start:] = body
                if len(fake.uploaded) >= fake.total:
                    status = fake.metadata["status"]
                    return self._send(201, {"id": "vid42", "status": {
                        "privacyStatus": status["privacyStatus"], "publishAt": status.get("publishAt", ""),
                        "uploadStatus": "uploaded"}})
                headers = {"Range": f"bytes=0-{len(fake.uploaded) - 1}"} if fake.uploaded else {}
                return self._send(308, None, headers)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def google(monkeypatch):
    fake = FakeGoogle()
    monkeypatch.setattr(oauth, "TOKEN_URL", fake.base + "/token")
    monkeypatch.setattr(oauth, "REVOKE_URL", fake.base + "/revoke")
    monkeypatch.setattr(api_mod, "API_URL", fake.base + "/youtube/v3")
    monkeypatch.setattr(api_mod, "UPLOAD_URL", fake.base + "/upload")
    monkeypatch.setattr(api_mod, "CAPTIONS_URL", fake.base + "/captions")
    monkeypatch.setattr(api_mod, "THUMBNAILS_URL", fake.base + "/thumbnails")
    monkeypatch.setattr(api_mod, "CHUNK", 256 * 1024)
    monkeypatch.setattr(api_mod, "PIECE", 64 * 1024)
    monkeypatch.setattr(api_mod.YouTubeClient, "_backoff", lambda self, r, why, cancel: r + 1)
    yield fake
    fake.close()


CLIENT = ClientConfig("123.apps.googleusercontent.com", "secret")


def _browser_that_approves(code="good-code", **extra):
    def open_browser(url):
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        assert query["code_challenge_method"] == "S256" and query["access_type"] == "offline"
        assert set(query["scope"].split()) == set(oauth.SCOPES)
        params = {"code": code, "state": query["state"], **extra}
        threading.Thread(
            target=lambda: urllib.request.urlopen(f"{query['redirect_uri']}?{urllib.parse.urlencode(params)}"),
            daemon=True,
        ).start()
    return open_browser


# ------------------------------------------------------------------ client file / storage
def test_client_config_from_file(tmp_path):
    good = tmp_path / "client.json"
    good.write_text(json.dumps({"installed": {"client_id": "a", "client_secret": "b"}}), encoding="utf-8")
    assert ClientConfig.from_file(good) == ClientConfig("a", "b")
    web = tmp_path / "web.json"
    web.write_text(json.dumps({"web": {"client_id": "a", "client_secret": "b"}}), encoding="utf-8")
    with pytest.raises(YouTubeError, match="Desktop app"):
        ClientConfig.from_file(web)
    with pytest.raises(YouTubeError):
        ClientConfig.from_file(tmp_path / "missing.json")


def test_without_secure_backend_tokens_stay_in_memory():
    store = default_store()  # the test machine has no keyring backend
    assert isinstance(store, MemoryStore) and not store.persistent
    store.set("k", {"refresh_token": "x"})
    assert store.get("k") == {"refresh_token": "x"}
    store.delete("k")
    assert store.get("k") is None


# ------------------------------------------------------------------ sign-in
def test_sign_in_connect_and_disconnect(google, data_dir):
    from app.config import AppConfig

    config = AppConfig.load(data_dir)
    store = MemoryStore()
    service = YouTubeService(config, store)
    with pytest.raises(YouTubeError, match="client file"):
        service.authorize(lambda url: None)
    service.set_client(CLIENT)
    creds, channels = service.authorize(_browser_that_approves())
    assert creds.refresh_token == "refresh-1"
    assert channels == [Channel("UC123", "Skof Clips", "@skofclips", "http://example/avatar.jpg")]
    assert channels[0].url == "https://www.youtube.com/@skofclips"
    service.add_account(creds, channels[0])
    assert service.active.id == "UC123" and service.status("UC123") == "Connected (this session only)"

    # Tokens are never in config.json.
    config.save()
    saved = Path(config.path).read_text(encoding="utf-8")
    assert "refresh-1" not in saved and "secret" not in saved and "UC123" in saved

    # A new session refreshes the access token from the stored refresh token.
    again = YouTubeService(AppConfig.load(data_dir), store)
    assert again.active.title == "Skof Clips"
    assert again.access_token("UC123") == "access-r1"
    assert again.client_for("UC123").my_channels()[0].id == "UC123"

    token = again.disconnect("UC123")
    assert token == "refresh-1" and again.accounts == [] and not again.has_token("UC123")
    oauth.revoke(token)
    assert google.revoked == ["refresh-1"]
    with pytest.raises(YouTubeAuthError):
        again.access_token("UC123")


def test_sign_in_denied_state_mismatch_and_cancel(google):
    with pytest.raises(AuthCancelled):
        oauth.sign_in(CLIENT, _browser_that_approves(error="access_denied"))

    def wrong_state(url):
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        threading.Thread(target=lambda: urllib.request.urlopen(
            f"{query['redirect_uri']}?code=good-code&state=forged"), daemon=True).start()

    with pytest.raises(YouTubeAuthError, match="state"):
        oauth.sign_in(CLIENT, wrong_state)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(AuthCancelled):
        oauth.sign_in(CLIENT, lambda url: None, cancel)
    with pytest.raises(YouTubeAuthError, match="timed out"):
        oauth.sign_in(CLIENT, lambda url: None, timeout_s=0.3)


def test_revoked_refresh_token_asks_to_reconnect(google):
    with pytest.raises(YouTubeAuthError, match="Reconnect"):
        oauth.refresh(CLIENT, "revoked")


# ------------------------------------------------------------------ metadata
def test_metadata_validation_and_body():
    future = datetime.now(timezone.utc) + timedelta(days=1)
    meta = VideoMetadata("Клип #shorts", "Описание", ("twitch", "sasavot"), "20", PUBLIC, future, "ru")
    assert meta.problems() == []
    body = meta.body()
    assert body["status"]["privacyStatus"] == PRIVATE  # scheduled videos must start private
    assert body["status"]["publishAt"].endswith("Z") and body["status"]["selfDeclaredMadeForKids"] is False
    assert body["snippet"]["defaultAudioLanguage"] == "ru" and body["snippet"]["tags"] == ["twitch", "sasavot"]

    bad = VideoMetadata("x" * 101, "<b>" + "я" * 2600, ("a" * 499, "b"), "99", "secret",
                        datetime.now(timezone.utc) - timedelta(minutes=1))
    problems = " ".join(bad.problems())
    for text in ("101 characters", "< or >", "bytes", "Tags are", "category", "visibility", "past"):
        assert text in problems
    assert VideoMetadata("  ").problems() == ["The title is empty."]
    assert parse_tags("#shorts, twitch ,Twitch,\nmy tag") == ("shorts", "twitch", "my tag")
    assert tags_length(("my tag", "ab")) == 8 + 1 + 2


# ------------------------------------------------------------------ upload
def _video(tmp_path, size=700 * 1024) -> Path:
    path = tmp_path / "short.mp4"
    path.write_bytes(bytes(range(256)) * (size // 256))
    return path


def test_resumable_upload_in_chunks_with_progress(google, tmp_path):
    path = _video(tmp_path)
    client = YouTubeClient(lambda force: "access-1")
    progress = []
    meta = VideoMetadata("Clip", privacy=PUBLIC)
    result = client.upload(path, meta, progress=lambda f, m: progress.append((f, m)))
    assert result.video_id == "vid42" and result.url == "https://youtube.com/shorts/vid42"
    assert bytes(google.uploaded) == path.read_bytes()
    assert google.metadata["snippet"]["title"] == "Clip" and google.puts == 3  # 3 chunks of 256 KiB
    assert progress[-1] == (1.0, "Uploaded") and any("MB/s" in m for _, m in progress)


def test_upload_resumes_after_network_drop_and_5xx(google, tmp_path):
    path = _video(tmp_path)
    google.drop_next_chunk = True
    result = YouTubeClient(lambda force: "access-1").upload(path, VideoMetadata("Clip"))
    assert result.video_id == "vid42" and bytes(google.uploaded) == path.read_bytes()

    google.uploaded.clear()
    google.fail_status = 503
    result = YouTubeClient(lambda force: "access-1").upload(path, VideoMetadata("Clip"))
    assert bytes(google.uploaded) == path.read_bytes()


def test_upload_refreshes_expired_token(google, tmp_path, data_dir):
    from app.config import AppConfig

    service = YouTubeService(AppConfig.load(data_dir), MemoryStore())
    service.set_client(CLIENT)
    creds = oauth.Credentials("stale", "refresh-1", time.time() + 3600)
    service.add_account(creds, Channel("UC123", "Skof Clips"))
    result = service.client_for("UC123").upload(_video(tmp_path), VideoMetadata("Clip"))
    assert result.video_id == "vid42" and google.refresh_count >= 1


def test_upload_errors_and_cancel(google, tmp_path):
    path = _video(tmp_path)
    google.quota = True
    with pytest.raises(YouTubeQuotaError, match="quota"):
        YouTubeClient(lambda force: "access-1").upload(path, VideoMetadata("Clip"))
    google.quota = False
    cancel = threading.Event()
    calls = []

    def progress(f, m):
        calls.append(f)
        if f > 0.1:
            cancel.set()

    with pytest.raises(UploadCancelled):
        YouTubeClient(lambda force: "access-1").upload(path, VideoMetadata("Clip"), progress, cancel)
    assert len(google.uploaded) < path.stat().st_size
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    with pytest.raises(YouTubeError, match="empty"):
        YouTubeClient(lambda force: "access-1").upload(empty, VideoMetadata("Clip"))


def test_upload_adds_captions_and_survives_missing_permission(google, tmp_path):
    from core.youtube.errors import YouTubeAuthError
    from ui.youtube.upload_dialog import _upload

    video = tmp_path / "v.mp4"
    video.write_bytes(b"x" * 300_000)
    client = YouTubeClient(lambda force: "access-1")
    srt = "1\n00:00:00,100 --> 00:00:01,000\nПРИВЕТ\n"
    meta = VideoMetadata(title="t", language="ru")
    result = _upload(client, video, meta, captions_srt=srt, captions_language="ru")
    assert result.video_id == "vid42" and result.captions == "added"
    body = google.captions[0]
    assert '"videoId": "vid42"' in body and '"language": "ru"' in body and "ПРИВЕТ" in body

    google.uploaded.clear()
    google.captions_scope_missing = True
    result = _upload(client, video, meta, captions_srt=srt)
    assert result.video_id == "vid42" and "reconnect" in result.captions.lower()
    with pytest.raises(YouTubeAuthError):
        client.upload_captions("vid42", srt, "ru")
    # An account connected without the captions permission still connects.
    assert set(oauth.REQUIRED_SCOPES) < set(oauth.SCOPES)


def test_upload_history(tmp_path):
    history = UploadHistory(tmp_path / "uploads.json")
    assert history.records() == []
    record = UploadRecord("2026-11-17T22:00", "C:/Shorts/a.mp4", "vid42", "https://youtube.com/shorts/vid42",
                          "Clip", "UC123", "Skof Clips", "public")
    history.add(record)
    assert history.records() == [record]
    assert history.for_file("c:/shorts/A.mp4") == [record]


# ------------------------------------------------------------------ UI
def _wait(predicate, timeout=15.0):
    from PySide6.QtWidgets import QApplication

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_accounts_dialog_connects_and_disconnects(qapp, google, data_dir, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from app.config import AppConfig
    from ui.youtube import accounts_dialog as dlg_mod

    monkeypatch.setattr(dlg_mod.webbrowser, "open", _browser_that_approves())
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    service = YouTubeService(AppConfig.load(data_dir), MemoryStore())
    dialog = dlg_mod.YouTubeAccountsDialog(service)
    # Connect is clickable before the client is loaded: it asks for the file instead of doing nothing.
    assert dialog.connect_button.isEnabled() and "Not set" in dialog.client_label.text()
    asked = []
    monkeypatch.setattr(dialog, "_ask_for_client", lambda: asked.append(1) or False)
    dialog.connect_button.click()
    assert asked and dialog._task is None
    dialog._open_guide()  # the guide opens inside the app
    assert "YouTube" in dialog.guide_dialog.findChild(dlg_mod.QTextBrowser).toPlainText()
    dialog.guide_dialog.close()
    client_file = tmp_path / "client_secret_x.json"
    client_file.write_text(json.dumps({"installed": CLIENT.to_dict()}), encoding="utf-8")
    # The Load button opens a file picker (clicked used to pass False as the path and fail silently).
    picked = []
    monkeypatch.setattr(dlg_mod.QFileDialog, "getOpenFileName",
                        lambda *a, **k: picked.append(1) or (str(client_file), ""))
    dialog.client_button.click()
    assert picked and service.client is not None
    assert dialog.load_client(str(client_file))
    assert dialog.connect_button.isEnabled()

    dialog.connect_button.click()
    assert dialog.cancel_button.isVisibleTo(dialog)
    assert _wait(lambda: dialog._task is None)
    assert service.active.id == "UC123"
    assert "✓ Skof Clips (@skofclips)" in dialog.list.item(0).text()

    dialog.list.setCurrentRow(0)
    dialog.disconnect_button.click()
    assert service.accounts == [] and _wait(lambda: google.revoked == ["refresh-1"])
    dialog.close()


def test_upload_dialog_validates_uploads_and_records(qapp, google, data_dir, tmp_path, ffmpeg_paths,
                                                     sample_clips):
    from app.config import AppConfig
    from ui.youtube.upload_dialog import UploadDialog, default_title

    service = YouTubeService(AppConfig.load(data_dir), MemoryStore())
    history = UploadHistory(tmp_path / "uploads.json")
    rendered = tmp_path / "my clip_short.mp4"
    rendered.write_bytes(Path(sample_clips["mp4"]).read_bytes())
    assert default_title(rendered) == "my clip"

    dialog = UploadDialog(service, history, rendered, ffmpeg_paths.ffprobe, {"privacy": "unlisted", "tags": "twitch"})
    assert "1280×720" in dialog.file_label.text() and "horizontal" in dialog.note_label.text()
    assert "Connect a YouTube channel" in dialog.problems_label.text() and not dialog.upload_button.isEnabled()

    service.set_client(CLIENT)
    service.add_account(oauth.Credentials("access-1", "refresh-1", time.time() + 3600), Channel("UC123", "Skof Clips"))
    dialog.refresh_channels()
    assert dialog.upload_button.isEnabled()
    dialog.title_edit.setText("x" * 120)
    assert "120 characters" in dialog.problems_label.text() and not dialog.upload_button.isEnabled()
    dialog.title_edit.setText("Лучший момент стрима #shorts")
    dialog.schedule_check.setChecked(True)
    assert dialog.privacy_combo.currentData() == "private" and not dialog.privacy_combo.isEnabled()
    dialog.schedule_check.setChecked(False)
    assert dialog.privacy_combo.currentData() == "private"
    dialog.privacy_combo.setCurrentIndex(dialog.privacy_combo.findData("public"))

    dialog.upload_button.click()
    assert dialog.cancel_button.isVisibleTo(dialog)
    assert _wait(lambda: dialog._task is None)
    assert dialog.result is not None and dialog.result.video_id == "vid42"
    assert "youtube.com/shorts/vid42" in dialog.status_label.text()
    assert google.metadata["snippet"]["title"] == "Лучший момент стрима #shorts"
    assert google.metadata["snippet"]["tags"] == ["twitch"]
    assert history.for_file(rendered)[0].video_id == "vid42"
    assert dialog.defaults()["privacy"] == "public"
    dialog.close()

    assert not dialog.captions_check.isEnabled() and not google.captions  # no subtitles given

    again = UploadDialog(service, history, rendered, ffmpeg_paths.ffprobe)
    assert "already uploaded" in again.status_label.text()
    again.close()

    from core.subtitles.model import Caption, CaptionWord

    caps = [Caption(100_000, 900_000, (CaptionWord("ПРИВЕТ", 100_000, 900_000, 100_000),))]
    with_subs = UploadDialog(service, history, rendered, ffmpeg_paths.ffprobe, captions=caps, captions_language="ru")
    assert with_subs.captions_check.isEnabled() and with_subs.captions_check.isChecked()
    with_subs.upload_button.click()
    assert _wait(lambda: with_subs._task is None)
    assert "captions" in with_subs.status_label.text() and "ПРИВЕТ" in google.captions[-1]
    with_subs.captions_check.setChecked(False)
    assert with_subs.defaults()["captions"] is False
    with_subs.close()


def test_main_window_refuses_to_upload_source(qapp, data_dir, ffmpeg_paths, sample_clips, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from app.config import AppConfig
    from ui.main_window.main_window import MainWindow

    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a[2]))
    w = MainWindow(config=AppConfig.load(data_dir), ffmpeg=ffmpeg_paths)
    w.import_files([str(sample_clips["mp4"])])
    assert _wait(lambda: w.timeline.timeline is not None)
    w.upload_to_youtube(Path(sample_clips["mp4"]))
    assert warnings and "source clip" in warnings[0]
    w._dirty = False
    w.close()


def test_upload_sets_thumbnail_and_explains_unverified_channel(google, tmp_path):
    from ui.youtube.upload_dialog import _upload

    video = tmp_path / "v.mp4"
    video.write_bytes(b"x" * 300_000)
    client = YouTubeClient(lambda force: "access-1")
    meta = VideoMetadata(title="t")
    result = _upload(client, video, meta, thumbnail=lambda: b"\xff\xd8jpeg")
    assert result.thumbnail == "set"
    assert google.thumbnails == [("vid42", "image/jpeg", b"\xff\xd8jpeg")]

    google.uploaded.clear()
    google.thumbnail_forbidden = True
    result = _upload(client, video, meta, thumbnail=lambda: b"\xff\xd8jpeg")
    assert result.video_id == "vid42" and "verif" in result.thumbnail
