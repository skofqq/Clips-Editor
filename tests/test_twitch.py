"""Import by Twitch link: link parsing, download through yt-dlp (faked) and metadata autofill."""

from __future__ import annotations

import shutil
import threading

import pytest

from core.metadata.generate import ClipMetadata
from core.metadata.library import GAME, STREAMER, Library
from core.twitch.autofill import autofill
from core.twitch.clip import ClipLink, TwitchClip, file_name, parse_clip_url
from core.twitch.download import DownloadCancelled, TwitchError, download_clip, fetch_info

INFO = {
    "id": "123",
    "title": "Невероятный клатч 1v5",
    "channel": "Evelone192",
    "creators": ["Evelone192"],
    "uploader": "some_viewer",
    "categories": ["Counter-Strike"],
    "timestamp": 1_763_000_000,  # 2025-11-13 UTC
    "duration": 28,
    "view_count": 1500,
}


@pytest.mark.parametrize("text, slug, login", [
    ("https://clips.twitch.tv/FunnyClipName-AbC_123", "FunnyClipName-AbC_123", ""),
    ("clips.twitch.tv/FunnyClipName?filter=clips", "FunnyClipName", ""),
    ("https://clips.twitch.tv/embed?clip=SlugX123&parent=x", "SlugX123", ""),
    ("https://www.twitch.tv/Evelone192/clip/SlugX123?filter=clips&range=7d", "SlugX123", "evelone192"),
    ("  <https://m.twitch.tv/evelone192/clip/SlugX123>  ", "SlugX123", "evelone192"),
])
def test_parse_clip_url(text, slug, login):
    assert parse_clip_url(text) == ClipLink(slug, login)


@pytest.mark.parametrize("text", [
    "", "hello", "https://www.twitch.tv/evelone192", "https://www.twitch.tv/videos/123456",
    "https://youtube.com/shorts/abc", "https://clips.twitch.tv/",
])
def test_not_a_clip_link(text):
    assert parse_clip_url(text) is None


def test_clip_from_ytdlp_and_file_name():
    clip = TwitchClip.from_ytdlp(INFO, ClipLink("SlugX123"))
    assert (clip.channel, clip.login, clip.game, clip.created) == (
        "Evelone192", "evelone192", "Counter-Strike", "2025-11-13")
    assert clip.channel_url == "https://www.twitch.tv/evelone192" and clip.clipper == "some_viewer"
    assert file_name(clip) == "Evelone192 - Невероятный клатч 1v5 [SlugX123].mp4"
    odd = TwitchClip(slug="s1", url="", channel="A", title='What?? <no> "way" / 100%')
    assert file_name(odd) == "A - What no way 100% [s1].mp4"
    # A Cyrillic display name is not a login; the link's login wins when present.
    ru = TwitchClip.from_ytdlp({**INFO, "channel": "Стример"}, ClipLink("S", ""))
    assert ru.login == "" and ru.channel_url == ""
    assert TwitchClip.from_ytdlp({**INFO, "channel": "Стример"}, ClipLink("S", "streamer")).login == "streamer"


class FakeYDL:
    """Stands in for yt_dlp.YoutubeDL."""

    calls: list[dict] = []
    fail: Exception | None = None

    def __init__(self, options: dict) -> None:
        self.options = options
        FakeYDL.calls.append(options)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url: str, download: bool = False):
        if FakeYDL.fail is not None:
            raise FakeYDL.fail
        if not download:
            return dict(INFO)
        template = self.options["outtmpl"]["default"]
        path = template.replace("%(ext)s", "mp4")
        for hook in self.options["progress_hooks"]:
            hook({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100})
        with open(path, "wb") as fh:
            fh.write(FakeYDL.payload)
        for hook in self.options["progress_hooks"]:
            hook({"status": "finished"})
        return {**INFO, "requested_downloads": [{"filepath": path}]}


@pytest.fixture
def fake_ydl():
    FakeYDL.calls = []
    FakeYDL.fail = None
    FakeYDL.payload = b"video"
    return FakeYDL


def test_download_clip(tmp_path, fake_ydl):
    seen = []
    path, clip = download_clip(ClipLink("SlugX123", "evelone192"), tmp_path / "clips",
                               progress=lambda f, m: seen.append(f), ydl_factory=fake_ydl)
    assert path.is_file() and path.name == "Evelone192 - Невероятный клатч 1v5 [SlugX123].mp4"
    assert clip.game == "Counter-Strike" and 0.5 in seen and seen[-1] == 1.0
    # The same clip again: the file is reused, nothing is downloaded.
    count = len(fake_ydl.calls)
    again, _ = download_clip(ClipLink("SlugX123", "evelone192"), tmp_path / "clips", ydl_factory=fake_ydl)
    assert again == path and len(fake_ydl.calls) == count + 1  # only the info request


def test_download_errors_and_cancel(tmp_path, fake_ydl):
    fake_ydl.fail = RuntimeError("ERROR: [twitch:clips] Clip does not exist")
    with pytest.raises(TwitchError, match="not found"):
        fetch_info(ClipLink("Gone1"), fake_ydl)
    fake_ydl.fail = None
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(DownloadCancelled):
        download_clip(ClipLink("SlugX123"), tmp_path, cancel=cancel, ydl_factory=fake_ydl)


def test_autofill_creates_and_reuses_profiles(tmp_path):
    library = Library(tmp_path / "lib.sqlite")
    clip = TwitchClip.from_ytdlp(INFO, ClipLink("SlugX123", "evelone192"))
    result = autofill(ClipMetadata(), clip, library)
    s = result.metadata.selection
    streamer = library.get(STREAMER, s.streamer_id)
    assert streamer.name == "Evelone192" and streamer.username == "evelone192"
    assert streamer.twitch_url == "https://www.twitch.tv/evelone192"
    assert library.get(GAME, s.game_id).name == "Counter-Strike"
    assert s.clip_title == "Невероятный клатч 1v5" and s.date == "2025-11-13"
    assert s.stream_url == "https://www.twitch.tv/evelone192"
    assert len(result.added) == 2 and result.missing == ()
    assert "Невероятный клатч" in result.metadata.title  # generated from the default template

    # Second clip of the same channel: existing profiles, nothing added.
    edited = result.metadata.__class__(**{**result.metadata.__dict__, "title": "Мой заголовок"})
    second = autofill(edited, TwitchClip.from_ytdlp({**INFO, "title": "Другой клип"}, ClipLink("S2")), library)
    assert second.added == () and second.metadata.selection.streamer_id == s.streamer_id
    assert second.metadata.title == "Мой заголовок"  # a hand-edited field is kept
    assert len(library.all(STREAMER)) == 1


def test_autofill_without_adding_to_library(tmp_path):
    library = Library(tmp_path / "lib.sqlite")
    library.create(STREAMER, "Evelone", username="evelone192")  # user's own profile without a Twitch link
    clip = TwitchClip.from_ytdlp(INFO, ClipLink("SlugX123", "evelone192"))
    result = autofill(ClipMetadata(), clip, library, add_to_library=False)
    streamer = library.get(STREAMER, result.metadata.selection.streamer_id)
    assert streamer.name == "Evelone" and streamer.twitch_url == "https://www.twitch.tv/evelone192"
    assert result.metadata.selection.game_id == "" and result.missing == ("game",)
    assert library.find(GAME, "Counter-Strike") is None


# ------------------------------------------------------------------ window

def test_window_imports_clip_by_link(qapp, data_dir, ffmpeg_paths, sample_clips, fake_ydl, monkeypatch, tmp_path):
    from app.config import AppConfig
    from tests.test_ui import _wait
    from ui.main_window.main_window import MainWindow
    from ui.twitch.import_dialog import TwitchImportDialog

    fake_ydl.payload = sample_clips["mp4"].read_bytes()  # a real playable video
    config = AppConfig.load(data_dir)
    config.twitch_folder = str(tmp_path / "twitch")
    w = MainWindow(config=config, ffmpeg=ffmpeg_paths)

    def run(dialog):  # press Import and wait for the (fake) download
        dialog.start()
        assert _wait(lambda: dialog.result is not None or "failed" in dialog.status.text())
        return dialog.DialogCode.Accepted if dialog.result else dialog.DialogCode.Rejected

    monkeypatch.setattr(TwitchImportDialog, "exec", run)
    assert w.import_from_twitch("https://www.twitch.tv/evelone192/clip/SlugX123", ydl_factory=fake_ydl)
    assert _wait(lambda: w.media_panel.media_count() == 1)
    media = w.media_panel.all_media()[0]
    assert media.path.parent == tmp_path / "twitch" and "[SlugX123]" in media.path.name
    selection = w.project.metadata.selection
    assert selection.clip_title == "Невероятный клатч 1v5" and selection.streamer_id and selection.game_id
    assert w.metadata_panel.metadata().selection.streamer_id == selection.streamer_id
    assert w.is_dirty
    w._set_dirty(False)
    w.close()


def test_dialog_validates_link(qapp):
    from ui.twitch.import_dialog import TwitchImportDialog

    d = TwitchImportDialog(url="not a link")
    assert not d.import_button.isEnabled() and "does not look" in d.status.text()
    d.url_edit.setText("https://clips.twitch.tv/SlugX123")
    assert d.import_button.isEnabled() and d.status.text() == ""
    d.info_only_check.setChecked(True)
    assert d.import_button.text() == "Fill in" and d.autofill
    d.close()


def test_twitch_settings_in_config(data_dir):
    from app.config import AppConfig

    config = AppConfig.load(data_dir)
    config.twitch_folder, config.twitch_autofill, config.twitch_add_to_library = "D:/clips", False, False
    config.save()
    loaded = AppConfig.load(data_dir)
    assert (loaded.twitch_folder, loaded.twitch_autofill, loaded.twitch_add_to_library) == ("D:/clips", False, False)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_icon_exists():
    from pathlib import Path

    assert (Path(__file__).resolve().parent.parent / "ui" / "theme" / "icons" / "link.svg").is_file()
