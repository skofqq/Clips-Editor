"""Updating the program from GitHub Releases (core.update + the update window)."""

from __future__ import annotations

import hashlib
import http.server
import json
import threading
import time
from pathlib import Path

import pytest

from core.update import github
from core.update.github import UpdateError, UpdateInfo, is_newer, parse_version, pick_update


def _release(tag, assets=(), draft=False, prerelease=False, body="notes"):
    return {"tag_name": tag, "name": f"TSS {tag}", "body": body, "draft": draft, "prerelease": prerelease,
            "html_url": f"https://github.com/x/y/releases/tag/{tag}", "published_at": "2026-11-22T00:00:00Z",
            "assets": list(assets)}


def _asset(name, url="http://x/a.exe", size=10, sha=""):
    item = {"name": name, "browser_download_url": url, "size": size}
    if sha:
        item["digest"] = f"sha256:{sha}"
    return item


def test_versions_compare_as_numbers():
    assert parse_version("v0.9.30") == (0, 9, 30)
    assert parse_version("0.9.6.5") == (0, 9, 6, 5)
    assert parse_version("1.0.0") == parse_version("v1.0") == (1,)
    assert parse_version("v1.2-beta") == (1, 2)
    assert parse_version("latest") == ()
    assert is_newer("0.9.31", "0.9.30")
    assert is_newer("v0.10.0", "0.9.30")  # not a string comparison
    assert is_newer("0.9.30.1", "0.9.30")
    assert not is_newer("0.9.30", "0.9.30")
    assert not is_newer("0.9.4", "0.9.30")
    assert not is_newer("nightly", "0.9.30")


def test_pick_update_takes_the_newest_real_release():
    releases = [
        _release("v0.9.29"),
        _release("v0.9.33", draft=True),
        _release("v0.9.32", prerelease=True),
        _release("v0.9.31", [_asset("TwitchShortsStudio-Setup-0.9.31.exe"),
                             _asset("TwitchShortsStudio-Update-0.9.31.exe", "http://h/u.exe", 1234, "AB" * 32)]),
        _release("v0.9.30"),
    ]
    info = pick_update(releases, "0.9.30")
    assert info.version == "0.9.31" and info.tag == "v0.9.31"
    assert info.asset_name == "TwitchShortsStudio-Update-0.9.31.exe"
    assert info.asset_url == "http://h/u.exe" and info.size == 1234
    assert info.sha256 == "ab" * 32 and info.installable and info.notes == "notes"
    assert pick_update(releases, "0.9.30", prerelease=True).version == "0.9.32"
    assert pick_update(releases, "0.9.31") is None
    assert pick_update([], "0.9.30") is None
    # A release without the update package is still reported (open its page).
    bare = pick_update([_release("v1.0.0", [_asset("TwitchShortsStudio-Setup-1.0.0.exe")])], "0.9.30")
    assert bare.version == "1.0.0" and not bare.installable


class _Server:
    """A tiny local stand-in for the GitHub API and the download host."""

    def __init__(self, payload: bytes, releases_status: int = 200) -> None:
        self.payload = payload
        self.releases_status = releases_status
        self.releases: list = []
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):  # quiet
                pass

            def do_GET(self):  # noqa: N802
                if self.path.startswith("/releases"):
                    body = json.dumps(server.releases).encode()
                    self.send_response(server.releases_status)
                elif self.path == "/asset.exe":
                    body = server.payload
                    self.send_response(200)
                else:
                    body = b""
                    self.send_response(404)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    payload = b"MZ fake installer " * 5000
    s = _Server(payload)
    s.releases = [_release("v99.0.0", [_asset("TwitchShortsStudio-Update-99.0.0.exe", s.url + "/asset.exe",
                                              len(payload), hashlib.sha256(payload).hexdigest())],
                           body="**Bold** news")]
    yield s
    s.close()


def test_check_and_download_from_the_releases_list(server, tmp_path):
    info = github.check_for_update("0.9.31", url=server.url + "/releases")
    assert info.version == "99.0.0" and info.installable
    seen = []
    path = github.download_update(info, tmp_path, progress=lambda f, m: seen.append(f))
    assert path.read_bytes() == server.payload and path.name == "TwitchShortsStudio-Update-99.0.0.exe"
    assert seen[-1] == 1.0 and not list(tmp_path.glob("*.part"))
    # Already downloaded and intact: kept, not fetched again.
    server.payload = b"changed"
    assert github.download_update(info, tmp_path) == path and path.read_bytes() != b"changed"


def test_damaged_download_is_thrown_away(server, tmp_path):
    info = github.check_for_update("0.9.31", url=server.url + "/releases")
    bad = UpdateInfo(**{**info.__dict__, "sha256": "0" * 64})
    with pytest.raises(UpdateError, match="damaged"):
        github.download_update(bad, tmp_path)
    short = UpdateInfo(**{**info.__dict__, "size": info.size + 10})
    with pytest.raises(UpdateError, match="stopped early"):
        github.download_update(short, tmp_path)
    assert not list(tmp_path.iterdir())
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(github.UpdateCancelled):
        github.download_update(info, tmp_path, cancel=cancel)
    with pytest.raises(UpdateError, match="no update package"):
        github.download_update(UpdateInfo("1", "v1", "t", "", "p"), tmp_path)


def test_github_errors_are_readable(server):
    server.releases_status = 403
    with pytest.raises(UpdateError, match="wait"):
        github.fetch_releases(server.url + "/releases")
    with pytest.raises(UpdateError, match="answered 404"):
        github.fetch_releases(server.url + "/nothing")
    with pytest.raises(UpdateError, match="Could not reach"):
        github.fetch_releases("http://127.0.0.1:9/releases", timeout=2)


def test_old_packages_are_cleaned_and_installer_switches(tmp_path):
    for name in ("TwitchShortsStudio-Update-0.9.29.exe", "TwitchShortsStudio-Update-0.9.30.exe.part",
                 "TwitchShortsStudio-Update-5.0.exe", "notes.txt"):
        (tmp_path / name).write_bytes(b"x")
    assert github.clean_downloads(tmp_path, "0.9.31") == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == ["TwitchShortsStudio-Update-5.0.exe", "notes.txt"]
    args = github.installer_arguments(Path("log.txt"))
    assert "/SILENT" in args and "/SUPPRESSMSGBOXES" in args and github.RELAUNCH_FLAG in args
    assert args[-1] == "/LOG=log.txt"
    assert not github.can_install_in_place()  # tests run from the source folder


def test_installer_script_builds_the_update_package():
    root = Path(__file__).resolve().parent.parent
    iss = (root / "packaging" / "installer.iss").read_text(encoding="utf-8")
    assert "TwitchShortsStudio-Update-{#AppVersion}" in iss and "#ifdef UpdateOnly" in iss
    assert "Check: Relaunch" in iss and "'/RELAUNCH'" in iss and github.RELAUNCH_FLAG == "/RELAUNCH"
    bat = (root / "build_installer.bat").read_text(encoding="utf-8")
    assert "/DUpdateOnly" in bat


def _wait(qapp, condition, seconds=10.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        qapp.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return False


def test_update_window_downloads_and_installs_after_closing(qapp, data_dir, ffmpeg_paths, server, monkeypatch):
    from app.config import AppConfig
    from ui.main_window import update_controller

    from tests.test_steps import _window

    monkeypatch.setenv("TSS_UPDATE_URL", server.url + "/releases")
    w = _window(data_dir, ffmpeg_paths)
    try:
        updates = w.updates
        assert updates.act_check in w.help_menu.actions() and updates.act_auto.isChecked()
        assert updates.badge.isHidden()
        updates.can_install = True  # pretend this is the installed program
        updates.check(manual=False)
        assert _wait(qapp, lambda: updates.dialog is not None)
        dialog = updates.dialog
        assert not updates.badge.isHidden() and "99.0.0" in updates.badge.text()
        assert "Bold" in dialog.notes.toPlainText() and dialog.main_button.text() == "Download and install"
        dialog.main_button.click()
        assert _wait(qapp, lambda: dialog.state == dialog.READY)
        assert updates.downloaded.read_bytes() == server.payload
        assert dialog.main_button.text() == "Install and restart"
        started = []
        monkeypatch.setattr(update_controller.github, "start_installer", lambda p, log: started.append(p) or True)
        dialog.main_button.click()
        assert not w.isVisible() and w.update_installer == updates.downloaded
        assert update_controller.run_installer(w) and started == [updates.downloaded]
    finally:
        w.close()
    # Turning the start-up check off is remembered.
    w = _window(data_dir, ffmpeg_paths)
    try:
        w.updates.act_auto.setChecked(False)
        assert AppConfig.load(data_dir).update_check is False
        assert not update_controller.run_installer(w)  # nothing downloaded
    finally:
        w.close()


def test_skipped_version_stays_quiet_and_source_copy_opens_the_page(qapp, data_dir, ffmpeg_paths, server, monkeypatch):
    from app.config import AppConfig

    from tests.test_steps import _window

    monkeypatch.setenv("TSS_UPDATE_URL", server.url + "/releases")
    config = AppConfig.load(data_dir)
    config.update_skipped = "99.0.0"
    config.save()
    w = _window(data_dir, ffmpeg_paths)
    try:
        updates = w.updates
        updates.check(manual=False)
        assert _wait(qapp, lambda: updates.info is not None)
        assert updates.dialog is None and not updates.badge.isHidden()  # no window, just the badge
        updates.badge.click()
        dialog = updates.dialog
        assert dialog.main_button.text() == "Open download page"  # running from source
        opened = []
        monkeypatch.setattr(updates, "open_page", lambda: opened.append(True))
        dialog.page_requested.disconnect()
        dialog.page_requested.connect(updates.open_page)
        dialog.main_button.click()
        assert opened
        dialog.skip_button.click()
        assert AppConfig.load(data_dir).update_skipped == "99.0.0" and updates.badge.isHidden()
    finally:
        w.close()
