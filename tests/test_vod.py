"""Download a part of a Twitch stream recording: links, times, playlist,
pieces (faked Twitch, real FFmpeg join), chat for Highlights and the window."""

from __future__ import annotations

import subprocess
import threading
import urllib.error
from pathlib import Path

import pytest

from core.twitch import vod
from core.twitch.download import DownloadCancelled, TwitchError

BASE = "https://cdn.example/abc/720p30/"


# ------------------------------------------------------------------ links and times
@pytest.mark.parametrize("text, vid, offset", [
    ("https://www.twitch.tv/videos/2345678901", "2345678901", 0),
    ("twitch.tv/videos/2345678901?t=1h20m5s", "2345678901", 4805),
    ("https://m.twitch.tv/videos/2345678901?filter=archives&t=15m", "2345678901", 900),
    ("https://www.twitch.tv/evelone192/v/2345678901", "2345678901", 0),
    ("  <https://www.twitch.tv/videos/2345678901>  ", "2345678901", 0),
    ("v2345678901", "2345678901", 0),
])
def test_parse_vod_link(text, vid, offset):
    assert vod.parse_vod_link(text) == vod.VodRef(vid, offset)


@pytest.mark.parametrize("text", ["", "hello", "https://clips.twitch.tv/Slug123456",
                                  "https://www.twitch.tv/evelone192/clip/Slug-1234567",
                                  "https://www.twitch.tv/evelone192"])
def test_not_a_vod_link(text):
    assert vod.parse_vod_link(text) is None


@pytest.mark.parametrize("text, seconds", [
    ("1:20:00", 4800), ("80:00", 4800), ("4800", 4800), ("1h20m", 4800), ("1ч 20м", 4800),
    ("0:00:05", 5), ("2:00:00", 7200), (" 1:05 ", 65), ("90m", 5400), ("1h", 3600),
])
def test_parse_time(text, seconds):
    assert vod.parse_time(text) == seconds


@pytest.mark.parametrize("text", ["", "abc", "1:61", "1:60:00", "-5"])
def test_bad_time(text):
    assert vod.parse_time(text) is None


def test_format_time():
    assert vod.format_time(4805.4) == "1:20:05" and vod.format_time(0) == "0:00:00"


# ------------------------------------------------------------------ facts from yt-dlp
INFO = {
    "id": "v2345678901",
    "title": "Катка с друзьями",
    "uploader": "Evelone192",
    "uploader_id": "evelone192",
    "timestamp": 1_763_000_000,
    "duration": 14_400,
    "formats": [
        {"format_id": "Audio_Only", "url": BASE + "audio.m3u8", "protocol": "m3u8_native", "vcodec": "none",
         "tbr": 160},
        {"format_id": "720p30", "url": BASE + "index-dvr.m3u8", "protocol": "m3u8_native", "height": 720,
         "fps": 30, "tbr": 3000},
        {"format_id": "1080p60", "url": "https://cdn.example/abc/chunked/index-dvr.m3u8",
         "protocol": "m3u8_native", "height": 1080, "fps": 60, "tbr": 8000, "format_note": "Source"},
        {"format_id": "360p30", "url": BASE + "360.m3u8", "protocol": "m3u8_native", "height": 360, "fps": 30},
        {"format_id": "storyboard", "url": "https://x/sb.json", "protocol": "mhtml"},
    ],
}


def test_info_from_ytdlp():
    info = vod.info_from_ytdlp(INFO, "2345678901")
    assert (info.channel, info.login, info.created, info.duration_s) == ("Evelone192", "evelone192",
                                                                        "2025-11-13", 14_400)
    assert [q.name for q in info.qualities] == ["1080p60", "720p30", "360p30", "Audio only"]
    assert info.qualities[0].source and info.qualities[0].label == "1080p60 (source)"
    assert info.quality("").name == "1080p60"
    assert info.quality("720p30").name == "720p30"
    assert info.quality("720p60").name == "720p30"  # nearest lower by height
    assert info.quality("480p30").name == "360p30"
    assert info.quality("audio only").name == "Audio only"
    assert round(info.qualities[1].size_mb(3600)) == 1287


class FakeYdl:
    def __init__(self, payload=None, error=None):
        self.payload, self.error, self.urls = payload, error, []

    def __call__(self, options):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        self.urls.append(url)
        if self.error:
            raise self.error
        return self.payload


def test_fetch_vod_info_and_errors():
    ydl = FakeYdl(INFO)
    info = vod.fetch_vod_info(vod.VodRef("2345678901"), ydl)
    assert ydl.urls == ["https://www.twitch.tv/videos/2345678901"] and info.title == "Катка с друзьями"
    with pytest.raises(TwitchError, match="subscribers"):
        vod.fetch_vod_info(vod.VodRef("1"), FakeYdl(error=RuntimeError("HTTP Error 403: Forbidden")))
    with pytest.raises(TwitchError, match="processing"):
        vod.fetch_vod_info(vod.VodRef("1"), FakeYdl({**INFO, "formats": []}))


# ------------------------------------------------------------------ playlist
PLAYLIST = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXT-X-PLAYLIST-TYPE:EVENT
#EXT-X-TWITCH-ELAPSED-SECS:0.000
#EXTINF:10.000,
0.ts
#EXTINF:10.000,
1-unmuted.ts
#EXTINF:10.000,
2.ts
#EXTINF:4.500,
3.ts
#EXT-X-ENDLIST
"""


def test_parse_playlist_and_pick():
    playlist = vod.parse_playlist(PLAYLIST, BASE + "index-dvr.m3u8")
    assert [s.url for s in playlist.segments] == [BASE + "0.ts", BASE + "1-unmuted.ts", BASE + "2.ts", BASE + "3.ts"]
    assert [s.start_s for s in playlist.segments] == [0, 10, 20, 30] and playlist.duration_s == 34.5
    assert [s.start_s for s in vod.pick_segments(playlist, 12, 25)] == [10, 20]
    assert [s.start_s for s in vod.pick_segments(playlist, 10, 20)] == [10]  # edges on piece bounds
    assert [s.start_s for s in vod.pick_segments(playlist, 25, None)] == [20, 30]
    assert vod.pick_segments(playlist, 40, None) == []
    fmp4 = vod.parse_playlist('#EXTM3U\n#EXT-X-MAP:URI="init-0.mp4"\n#EXTINF:2.0,\n0.mp4\n', BASE + "x.m3u8")
    assert fmp4.init_url == BASE + "init-0.mp4" and len(fmp4.segments) == 1
    with pytest.raises(TwitchError):
        vod.parse_playlist("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nhttps://x/720.m3u8\n", BASE)


def test_file_name():
    info = vod.info_from_ytdlp({**INFO, "title": 'Катка: "с" друзьями?'}, "2345678901")
    assert vod.file_name(info, 4800, 7200) == "Evelone192 - Катка с друзьями [v2345678901] 1-20-00–2-00-00.mp4"


# ------------------------------------------------------------------ download (real FFmpeg join)
@pytest.fixture(scope="module")
def pieces(tmp_path_factory):
    """Four 2-second MPEG-TS pieces of a real video, like Twitch serves them."""
    import shutil

    if shutil.which("ffmpeg") is None:
        pytest.skip("needs ffmpeg")
    folder = tmp_path_factory.mktemp("hls")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc2=size=320x180:rate=30:duration=8", "-f", "lavfi", "-i",
                    "sine=frequency=440:sample_rate=48000:duration=8", "-c:v", "libx264", "-g", "60",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", "-f", "hls", "-hls_time", "2",
                    "-hls_list_size", "0", "-hls_segment_filename", str(folder / "%d.ts"),
                    str(folder / "index.m3u8")], check=True)
    text = (folder / "index.m3u8").read_text().replace("1.ts", "1-unmuted.ts")
    files = {BASE + p.name: p.read_bytes() for p in folder.glob("*.ts")}
    files[BASE + "1-muted.ts"] = files.pop(BASE + "1.ts")  # Twitch muted this piece: only -muted exists
    files[BASE + "index-dvr.m3u8"] = text.encode()
    return files


class FakeCdn:
    def __init__(self, files, fail=(), cancel_after=None, cancel=None):
        self.files, self.fail, self.got = files, set(fail), []
        self.cancel_after, self.cancel = cancel_after, cancel
        self.lock = threading.Lock()

    def __call__(self, url):
        with self.lock:
            self.got.append(url)
            if self.cancel_after is not None and len(self.got) > self.cancel_after:
                self.cancel.set()
        if url in self.fail:
            raise urllib.error.URLError("connection reset")
        if url not in self.files:
            raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
        return self.files[url]


def _vod_info():
    return vod.VodInfo("2345678901", "Катка", "Evelone192", "evelone192", 8.0, "2025-11-13",
                       (vod.VodQuality("720p30", BASE + "index-dvr.m3u8", 720, 3000, True),))


def _duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                          str(path)], capture_output=True, text=True, check=True).stdout
    return float(out.strip())


def test_download_part(pieces, tmp_path):
    cdn = FakeCdn(pieces)
    seen = []
    result = vod.download_vod(_vod_info(), 3, 5, tmp_path, "ffmpeg", get=cdn,
                              progress=lambda f, t: seen.append((f, t)))
    assert (result.start_s, result.end_s) == (2.0, 6.0)  # widened to whole pieces
    assert result.path.name == "Evelone192 - Катка [v2345678901] 0-00-02–0-00-06.mp4"
    assert abs(_duration(result.path) - 4.0) < 0.3
    assert BASE + "0.ts" not in cdn.got and BASE + "3.ts" not in cdn.got  # only the needed pieces
    assert BASE + "1-muted.ts" in cdn.got  # the -unmuted name failed, -muted was used
    assert seen[-1][0] == 1.0 and any("pieces" in t for _f, t in seen)
    assert not list(tmp_path.glob(".parts*"))  # the pieces are cleaned up
    # Same range again: the file is reused.
    again = vod.download_vod(_vod_info(), 3, 5, tmp_path, "ffmpeg", get=FakeCdn(pieces))
    assert again.path == result.path


def test_cancel_keeps_pieces_and_resumes(pieces, tmp_path):
    cancel = threading.Event()
    cdn = FakeCdn(pieces, cancel_after=2, cancel=cancel)  # playlist + 1 piece, then the user cancels
    with pytest.raises(DownloadCancelled):
        vod.download_vod(_vod_info(), 0, None, tmp_path, "ffmpeg", get=cdn, cancel=cancel, workers=1)
    parts = list(tmp_path.glob(".parts*"))
    assert len(parts) == 1 and list(parts[0].glob("*.ts"))
    kept = len(list(parts[0].glob("0*.ts")))
    resumed = FakeCdn(pieces)
    result = vod.download_vod(_vod_info(), 0, None, tmp_path, "ffmpeg", get=resumed)
    assert len([u for u in resumed.got if u.endswith(".ts")]) <= 4 - kept + 1  # (+1: the -unmuted try)
    assert abs(_duration(result.path) - 8.0) < 0.3


def test_failed_piece_is_an_error_not_a_cancel(pieces, tmp_path, monkeypatch):
    monkeypatch.setattr(vod.time, "sleep", lambda s: None)
    cancel = threading.Event()
    cdn = FakeCdn(pieces, fail={BASE + "2.ts"})
    with pytest.raises(TwitchError, match="piece"):
        vod.download_vod(_vod_info(), 0, None, tmp_path, "ffmpeg", get=cdn, cancel=cancel)
    assert not cancel.is_set()


def test_range_past_the_end(pieces, tmp_path):
    with pytest.raises(TwitchError, match="only 0:00:08 long"):
        vod.download_vod(_vod_info(), 60, None, tmp_path, "ffmpeg", get=FakeCdn(pieces))


# ------------------------------------------------------------------ chat + the whole job
def _chat_post(sent):
    def post(body):
        sent.append(body[0]["variables"])
        return [{"data": {"video": {"comments": {
            "edges": [{"cursor": "c1", "node": {"id": "1", "contentOffsetSeconds": 2.5,
                                                "commenter": {"displayName": "a"},
                                                "message": {"fragments": [{"text": "KEKW"}]}}},
                      {"cursor": "c2", "node": {"id": "2", "contentOffsetSeconds": 5.0,
                                                "commenter": {"displayName": "b"},
                                                "message": {"fragments": [{"text": "Pog"}]}}},
                      {"cursor": "c3", "node": {"id": "3", "contentOffsetSeconds": 7.0,
                                                "commenter": {"displayName": "c"},
                                                "message": {"fragments": [{"text": "later"}]}}}],
            "pageInfo": {"hasNextPage": False}}}}}]
    return post


def test_run_download_with_chat_and_facts(pieces, tmp_path):
    from core.highlights import chat as chat_mod
    from core.metadata import twitch_link as tl
    from ui.twitch.vod_dialog import run_download

    sent, asked = [], []

    def link_info(url):
        asked.append(url)
        return tl.TwitchInfo(tl.parse_link(url), "evelone192", "Evelone192", "Counter-Strike")

    result, facts, notes = run_download(
        _vod_info(), 3, 5, tmp_path / "vods", "ffmpeg", "", True, tmp_path / "cache", True,
        progress=lambda f, t: None, cancel=threading.Event(), get=FakeCdn(pieces), post=_chat_post(sent),
        link_info=link_info)
    assert sent == [{"videoID": "2345678901", "contentOffsetSeconds": 2}]
    messages, source = chat_mod.load_chat(result.path, tmp_path / "cache")
    assert [(m.t, m.text) for m in messages] == [(0.5, "KEKW"), (3.0, "Pog")]  # from the file's start
    assert "twitch.tv/videos/2345678901" in source
    assert asked == ["https://www.twitch.tv/videos/2345678901?t=2s"] and facts.game == "Counter-Strike"
    assert any("2 messages" in n for n in notes)


def test_chat_failure_keeps_the_video(pieces, tmp_path):
    from ui.twitch.vod_dialog import run_download

    def post(body):
        return [{"errors": [{"message": "PersistedQueryNotFound"}]}]

    result, facts, notes = run_download(
        _vod_info(), 0, None, tmp_path, "ffmpeg", "", True, tmp_path / "cache", False,
        progress=lambda f, t: None, cancel=threading.Event(), get=FakeCdn(pieces), post=post)
    assert result.path.is_file() and facts is None
    assert any("chat was not downloaded" in n for n in notes)


# ------------------------------------------------------------------ settings and window
def test_vod_settings_in_config(data_dir):
    from app.config import AppConfig

    config = AppConfig.load(data_dir)
    config.vod_folder, config.vod_quality, config.vod_chat, config.vod_fill = "D:/vods", "720p30", False, False
    config.save()
    loaded = AppConfig.load(data_dir)
    assert (loaded.vod_folder, loaded.vod_quality, loaded.vod_chat, loaded.vod_fill) == ("D:/vods", "720p30",
                                                                                      False, False)


def test_dialog_reads_link_and_checks_times(qapp):
    from tests.test_ui import _wait
    from ui.twitch.vod_dialog import VodDownloadDialog

    d = VodDownloadDialog(url="not a link", ffmpeg="ffmpeg", ydl_factory=FakeYdl(INFO), quality="720p30")
    assert "does not look" in d.info_label.text() and not d.download_button.isEnabled()
    d.url_edit.setText("https://www.twitch.tv/videos/2345678901?t=1h20m")
    assert d.start_edit.text() == "1:20:00"
    assert _wait(lambda: d.info is not None)
    assert "Evelone192" in d.info_label.text() and "4:00:00" in d.info_label.text()
    assert d.quality == "720p30" and d.quality_combo.count() == 4
    d.end_edit.setText("2:00:00")
    assert d.download_button.isEnabled() and "0:40:00 of video" in d.summary.text()
    assert "GB" in d.summary.text() or "MB" in d.summary.text()
    d.end_edit.setText("1:00:00")
    assert not d.download_button.isEnabled() and "after the start" in d.summary.text()
    d.end_edit.setText("soon")
    assert "not readable" in d.summary.text()
    d.start_edit.setText("5:00:00")
    d.end_edit.clear()
    assert "only 4:00:00 long" in d.summary.text()
    d._whole()
    assert d.download_button.isEnabled() and "4:00:00 of video" in d.summary.text()
    d.close()


def test_window_downloads_part_with_chat(qapp, data_dir, ffmpeg_paths, pieces, tmp_path):
    from app.config import AppConfig
    from core.metadata import twitch_link as tl
    from tests.test_ui import _wait
    from ui.main_window.main_window import MainWindow

    info = {**INFO, "duration": 8, "formats": [INFO["formats"][1]]}
    config = AppConfig.load(data_dir)
    config.vod_folder = str(tmp_path / "vods")
    w = MainWindow(config=config, ffmpeg=ffmpeg_paths)
    facts = tl.TwitchInfo(tl.parse_link("https://www.twitch.tv/videos/2345678901"), "evelone192",
                          "Evelone192", "Counter-Strike", "", "Катка", "13.11.2025",
                          "https://www.twitch.tv/videos/2345678901")
    # A VOD link given to "Import from Twitch" opens the recording window instead.
    d = w.download_vod("https://www.twitch.tv/videos/2345678901?t=3s", ydl_factory=FakeYdl(info),
                       get=FakeCdn(pieces), post=_chat_post([]), link_info=lambda url: facts)
    assert d is not None and w.download_vod() is d  # one window at a time
    assert _wait(lambda: d.info is not None)
    d.end_edit.setText("0:00:05")
    assert d.start()
    assert w.act_vod.text().startswith("Download Stream")
    assert _wait(lambda: w.media_panel.media_count() == 1, 20)
    media = w.media_panel.all_media()[0]
    assert media.path.parent == tmp_path / "vods" and "[v2345678901]" in media.path.name
    assert _wait(lambda: w.preview.media is not None and w.preview.media.path == media.path)
    assert _wait(lambda: len(w.highlights.chat) == 2)  # the chat is there for Highlights
    selection = w.metadata_panel.metadata().selection
    assert selection.streamer_id and selection.game_id  # Data page filled from Twitch
    assert w.vod_dialog is None and w.config.vod_quality == "720p30"
    w._set_dirty(False)
    w.close()


def test_import_dialog_points_to_vod_window(qapp):
    from ui.twitch.import_dialog import TwitchImportDialog

    d = TwitchImportDialog(url="https://www.twitch.tv/videos/2345678901")
    assert "Download Stream Recording" in d.status.text()
    d.close()
