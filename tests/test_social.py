"""Stage 11: TikTok and Instagram Reels, against fake servers."""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from core.social import instagram, tiktok
from core.social.errors import SocialAuthError, SocialError
from core.social.service import SocialService
from core.youtube.storage import MemoryStore


class FakeSocial:
    def __init__(self) -> None:
        fake = self
        self.uploaded = bytearray()
        self.ranges: list[str] = []
        self.inits: list[dict] = []
        self.ig_bytes = b""
        self.ig_form: dict = {}
        self.ig_polls = 0
        self.token_forms: list[dict] = []
        self.unaudited = False
        self.ig_bad_token = False

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            def do_POST(self):
                url = urllib.parse.urlparse(self.path)
                body = self._body()
                ok = {"error": {"code": "ok", "message": ""}}
                if url.path == "/tt/oauth/token/":
                    form = dict(urllib.parse.parse_qsl(body.decode()))
                    fake.token_forms.append(form)
                    return self._send(200, {"access_token": "tt-access", "refresh_token": "tt-refresh",
                                            "expires_in": 86400, "refresh_expires_in": 31536000,
                                            "open_id": "open1", "scope": "user.info.basic,video.publish"})
                if url.path == "/tt/post/publish/creator_info/query/":
                    return self._send(200, {**ok, "data": {
                        "creator_username": "skof", "creator_nickname": "Skof",
                        "privacy_level_options": ["PUBLIC_TO_EVERYONE", "SELF_ONLY"],
                        "max_video_post_duration_sec": 600}})
                if url.path in ("/tt/post/publish/video/init/", "/tt/post/publish/inbox/video/init/"):
                    data = json.loads(body)
                    fake.inits.append({"path": url.path, **data})
                    if fake.unaudited and "post_info" in data and data["post_info"]["privacy_level"] != "SELF_ONLY":
                        return self._send(403, {"error": {
                            "code": "unaudited_client_can_only_post_to_private_accounts", "message": "x"}})
                    host = self.headers["Host"]
                    return self._send(200, {**ok, "data": {"publish_id": "p1", "upload_url": f"http://{host}/put"}})
                if url.path == "/tt/post/publish/status/fetch/":
                    inbox = any(i["path"].endswith("inbox/video/init/") for i in fake.inits[-1:])
                    return self._send(200, {**ok, "data": {
                        "status": "SEND_TO_USER_INBOX" if inbox else "PUBLISH_COMPLETE",
                        "publicaly_available_post_id": [] if inbox else [7311]}})
                # Instagram
                if url.path == "/ig/v21.0/17841/media":
                    fake.ig_form = dict(urllib.parse.parse_qsl(body.decode()))
                    host = self.headers["Host"]
                    return self._send(200, {"id": "c1", "uri": f"http://{host}/rupload/c1"})
                if url.path == "/rupload/c1":
                    assert self.headers["Authorization"] == "OAuth ig-token"
                    assert self.headers["offset"] == "0" and int(self.headers["file_size"]) == len(body)
                    fake.ig_bytes = body
                    return self._send(200, {"success": True})
                if url.path == "/ig/v21.0/17841/media_publish":
                    return self._send(200, {"id": "m9"})
                self._send(404, {"error": "nope"})

            def do_PUT(self):
                body = self._body()
                fake.ranges.append(self.headers["Content-Range"])
                start = int(self.headers["Content-Range"].split()[1].split("-")[0])
                fake.uploaded[start:start + len(body)] = body
                self._send(201, {})

            def do_GET(self):
                url = urllib.parse.urlparse(self.path)
                q = dict(urllib.parse.parse_qsl(url.query))
                if url.path == "/tt/user/info/":
                    return self._send(200, {"error": {"code": "ok"}, "data": {"user": {"display_name": "Skof"}}})
                if url.path == "/ig/v21.0/me":
                    if fake.ig_bad_token or q.get("access_token") != "ig-token":
                        return self._send(400, {"error": {"message": "Invalid OAuth access token",
                                                          "type": "OAuthException", "code": 190}})
                    return self._send(200, {"user_id": "17841", "username": "skof.clips",
                                            "account_type": "MEDIA_CREATOR"})
                if url.path == "/ig/v21.0/c1":
                    fake.ig_polls += 1
                    return self._send(200, {"status_code": "IN_PROGRESS" if fake.ig_polls < 2 else "FINISHED"})
                if url.path == "/ig/v21.0/m9":
                    return self._send(200, {"permalink": "https://www.instagram.com/reel/abc/"})
                if url.path == "/ig/refresh_access_token":
                    return self._send(200, {"access_token": "ig-token-2", "expires_in": 5184000})
                self._send(404, {"error": "nope"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake(monkeypatch):
    server = FakeSocial()
    monkeypatch.setattr(tiktok, "API_URL", server.base + "/tt")
    monkeypatch.setattr(instagram, "GRAPH_URL", server.base + "/ig")
    yield server
    server.close()


class Config:
    def __init__(self):
        self.social_accounts = {}
        self.social_defaults = {}


def _video(tmp_path, size=300_000):
    path = tmp_path / "short.mp4"
    path.write_bytes(bytes(range(256)) * (size // 256))
    return path


def test_tiktok_sign_in_with_pkce(fake):
    app = tiktok.TikTokApp("key1", "secret1")

    def browser(url):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        assert q["client_key"] == "key1" and q["code_challenge_method"] == "S256"
        assert len(q["code_challenge"]) == 64 and "video.publish" in q["scope"]
        back = f"{q['redirect_uri']}?code=c0de&state={q['state']}"
        threading.Thread(target=lambda: urllib.request.urlopen(back, timeout=5).read(), daemon=True).start()

    token = tiktok.sign_in(app, browser, port=0)
    assert token.access_token == "tt-access" and token.open_id == "open1"
    form = fake.token_forms[-1]
    assert form["code"] == "c0de" and form["grant_type"] == "authorization_code" and len(form["code_verifier"]) >= 43
    assert tiktok.user_name(token.access_token) == "Skof"


def test_tiktok_chunk_rules():
    assert tiktok.chunks(3_000_000) == (3_000_000, 1)
    assert tiktok.chunks(64 * 1024 * 1024) == (64 * 1024 * 1024, 1)
    size, count = tiktok.chunks(95 * 1024 * 1024)
    assert size == 10 * 1024 * 1024 and count == 9  # the last chunk takes the 5 MB remainder


def test_tiktok_direct_post_and_drafts(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(tiktok, "CHUNK", 100_000)
    monkeypatch.setattr(tiktok, "MAX_CHUNK", 120_000)
    video = _video(tmp_path)
    post = tiktok.TikTokPost(caption="ОХОТА #sasavot", privacy="PUBLIC_TO_EVERYONE", cover_ms=1500)
    result = tiktok.post_video("tt-access", video, post, poll_s=0, username="skof")
    assert bytes(fake.uploaded) == video.read_bytes()
    assert fake.ranges[0] == f"bytes 0-99999/{video.stat().st_size}" and len(fake.ranges) == 2
    info = fake.inits[-1]["post_info"]
    assert info["title"] == "ОХОТА #sasavot" and info["video_cover_timestamp_ms"] == 1500
    assert result.url == "https://www.tiktok.com/@skof/video/7311"

    draft = tiktok.post_video("tt-access", video, tiktok.TikTokPost(mode=tiktok.INBOX), poll_s=0)
    assert "post_info" not in fake.inits[-1] and "inbox" in draft.message.lower()

    fake.unaudited = True
    with pytest.raises(SocialError, match="Only me"):
        tiktok.post_video("tt-access", video, post, poll_s=0)


def test_instagram_connect_post_and_refresh(fake, tmp_path):
    account = instagram.connect("ig-token")
    assert account.username == "skof.clips" and account.user_id == "17841"
    with pytest.raises(SocialAuthError, match="token"):
        instagram.connect("bad")
    video = _video(tmp_path)
    progress = []
    result = instagram.post_reel(account, video, "ОХОТА #sasavot", share_to_feed=False, cover_ms=2000,
                                 progress=lambda f, m: progress.append(m), poll_s=0)
    assert fake.ig_bytes == video.read_bytes()
    assert fake.ig_form["media_type"] == "REELS" and fake.ig_form["upload_type"] == "resumable"
    assert fake.ig_form["share_to_feed"] == "false" and fake.ig_form["thumb_offset"] == "2000"
    assert result.url == "https://www.instagram.com/reel/abc/" and any("processing" in m for m in progress)

    assert instagram.refresh(account) is account  # fresh token: unchanged
    old = instagram.InstagramAccount("17841", "skof.clips", "ig-token", time.time() - 10 * 86400)
    assert instagram.refresh(old).token == "ig-token-2"
    expired = instagram.InstagramAccount("17841", "x", "t", time.time() - 61 * 86400)
    with pytest.raises(SocialAuthError):
        instagram.refresh(expired)


def test_service_keeps_secrets_out_of_config(fake, tmp_path):
    config, store = Config(), MemoryStore()
    social = SocialService(config, store)
    assert social.tiktok_app is None and social.tiktok_name == "" and social.instagram_name == ""
    social.set_tiktok_app(" key ", " secret ")
    token = tiktok.TikTokToken("tt-access", "tt-refresh", time.time() - 10, time.time() + 1000, "open1")
    social.tiktok_connected(token, "Skof")
    assert social.tiktok_name == "Skof" and "secret" not in json.dumps(config.social_accounts)
    assert social.tiktok_access_token() == "tt-access"  # expired → refreshed
    assert fake.token_forms[-1]["grant_type"] == "refresh_token"
    social.instagram_connected(instagram.connect("ig-token"))
    assert social.instagram_name == "@skof.clips" and "ig-token" not in json.dumps(config.social_accounts)
    social.disconnect_instagram()
    social.disconnect_tiktok()
    assert social.instagram_name == "" and social.tiktok_name == "" and config.social_accounts == {}


def test_default_caption():
    from ui.social.crosspost_panel import default_caption

    caption = default_caption("ОГРАБЛЕНИЕ/САСЫЧА | #sasavot on #Twitch", "#shorts #sasavot #twitch\n\nhttps://x")
    assert caption == "ОГРАБЛЕНИЕ/САСЫЧА #sasavot #Twitch #shorts"


def test_publish_page_cross_posts(qapp, data_dir, ffmpeg_paths, fake, tmp_path, monkeypatch):
    from tests.test_steps import _wait, _window
    from ui.main_window.pages import PUBLISH
    from ui.social.guide_panel import INSTAGRAM, TIKTOK

    monkeypatch.setattr(tiktok, "post_video", lambda token, path, post, progress=None, cancel=None, **kw:
                        tiktok.TikTokResult("p1", "PUBLISH_COMPLETE", "https://www.tiktok.com/@skof/video/1",
                                            "Posted on TikTok."))
    w = _window(data_dir, ffmpeg_paths)
    try:
        w.social.store = MemoryStore()
        panel, reels = w.tiktok_panel, w.reels_panel
        w.set_page(PUBLISH)
        # One tab per service, each with its logo.
        bar = w.service_tabs.bar
        assert [bar.tabText(i) for i in range(bar.count())] == ["YouTube", "TikTok", "Instagram Reels", "Schedule", "History"]
        assert all(not bar.tabIcon(i).isNull() for i in range(bar.count()))
        tabs = w.service_tabs.pages
        assert not tabs[0].isHidden() and panel.isHidden()
        w.service_tabs.set_current(1)
        assert not panel.isHidden() and tabs[0].isHidden() and reels.isHidden()
        assert not panel.post_button.isEnabled() and panel.tiktok_status.text() == "Not set up"
        # The guide opens on the side, next to the page.
        panel.tiktok_guide.click()
        assert not w.guide_panel.isHidden() and w.guide_panel.service == TIKTOK
        assert "Content Posting API" in w.guide_panel.view.toPlainText()
        w.service_tabs.set_current(2)
        reels.reels_guide.click()
        assert w.guide_panel.service == INSTAGRAM
        setup = w.guide_panel.instagram_setup
        setup.token_edit.setText("ig-token")
        setup.connect_button.click()
        assert _wait(lambda: w.social.instagram_name == "@skof.clips")
        assert reels.reels_status.text() == "@skof.clips"
        w.social.set_tiktok_app("k", "s")
        w.social.tiktok_connected(tiktok.TikTokToken("tt-access", "r", time.time() + 9999, time.time() + 9999,
                                                     "o"), "Skof")
        panel.refresh_accounts()
        assert _wait(lambda: panel.privacy_combo.count() == 2)
        video = _video(tmp_path)
        for p in (panel, reels):
            p.set_file(video)
            p.set_caption_source("ОХОТА | #sasavot", "#shorts")
            assert p.caption_edit.toPlainText() == "ОХОТА #sasavot #shorts"
            assert p.post_button.isEnabled()
        results = []
        panel.posted.connect(results.append)
        reels.posted.connect(results.append)
        panel.post_button.click()
        assert _wait(lambda: results, 20)
        assert [(r.service, r.ok) for r in results[0]] == [(TIKTOK, True)], [r.message for r in results[0]]
        assert "tiktok.com/@skof" in panel.result_label.text()
        reels.feed_check.setChecked(False)
        reels.post_button.click()
        assert _wait(lambda: len(results) == 2, 20)
        assert [(r.service, r.ok) for r in results[1]] == [(INSTAGRAM, True)], [r.message for r in results[1]]
        assert "instagram.com/reel/abc" in reels.result_label.text()
        assert w.config.social_defaults["share_to_feed"] is False
        assert w.config.social_defaults["tiktok_mode"] == tiktok.DIRECT
        # Both posts are in the History tab, newest first.
        services = [p.service for p in w.clip_memory.publications()]
        assert sorted(services) == ["instagram", "tiktok"]
        assert w.published_panel.list.count() == 2
        w.guide_panel.hide()
    finally:
        w._set_dirty(False)
        w.close()
