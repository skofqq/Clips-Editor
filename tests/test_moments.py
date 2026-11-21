"""Best moments in a long recording (0.9.28): chat, Shorts-length windows,
review ticks, saving the ticked moments as clips."""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

import numpy as np
import pytest

from core.audio.waveform import Waveform
from core.highlights import chat as chat_mod
from core.highlights.chat import ChatMessage, chat_events, message_kind, parse_chat, parse_vod_url
from core.highlights.clips import ClipCancelled, clip_name, save_clips, slice_transcript
from core.highlights.finder import CHAT, CHAT_HYPE, CHAT_LAUGH, LAUGH, LOUD, Event, Highlight
from core.highlights.moments import (
    AUTO,
    default_length,
    default_limit,
    find_moments,
    merge_review,
    pick_windows,
    quote,
    snap,
)
from core.projects.project import Project
from core.transcription.model import WORD_TIMING, Segment, Transcript, Word

S = 1_000_000
PPS = 100


def _peaks(seconds: float, level: float, bursts) -> np.ndarray:
    rng = np.random.default_rng(1)
    x = np.full(int(seconds * PPS), level) * rng.uniform(0.6, 1.0, int(seconds * PPS))
    for start, end, peak in bursts:
        x[int(start * PPS):int(end * PPS)] = peak
    return np.clip(x * 255, 0, 255).astype(np.uint8)


def _transcript(sentences, duration_s=600) -> Transcript:
    """sentences: (start s, end s, text); words spread evenly."""
    segs = []
    for i, (a, b, text) in enumerate(sentences):
        tokens = text.split()
        step = (b - a) / len(tokens)
        words = tuple(Word(int((a + k * step) * S), int((a + (k + 1) * step) * S), t, 0.9, WORD_TIMING)
                      for k, t in enumerate(tokens))
        segs.append(Segment(i + 1, int(a * S), int(b * S), text, words))
    return Transcript("ru", 1.0, "test", tuple(segs), duration_s * S)


def _chat(duration_s, bursts, base_every=4.0):
    """Slow chat (a message every few seconds) with bursts (t, n, text)."""
    msgs = [ChatMessage(t, "привет", "a") for t in np.arange(0, duration_s, base_every)]
    for t, n, text in bursts:
        msgs += [ChatMessage(t + k * 3.0 / n, text, f"u{k}") for k in range(n)]
    return sorted(msgs, key=lambda m: m.t)


# ------------------------------------------------------------------ chat files
def test_parse_twitchdownloader_json_and_offset(tmp_path):
    data = {"video": {"start": 100, "end": 400},
            "comments": [{"content_offset_seconds": 160.5, "commenter": {"display_name": "Bob"},
                          "message": {"body": "KEKW"}},
                         {"content_offset_seconds": 130, "commenter": {"display_name": "Ann"},
                          "message": {"fragments": [{"text": "Pog "}, {"text": "wow"}]}}]}
    file = tmp_path / "chat.json"
    file.write_text(json.dumps(data), encoding="utf-8")
    msgs = chat_mod.read_chat_file(file)
    assert [(m.t, m.text, m.author) for m in msgs] == [(30.0, "Pog wow", "Ann"), (60.5, "KEKW", "Bob")]


def test_parse_other_layouts():
    lines = "\n".join(json.dumps({"time_in_seconds": t, "message": "lol", "author": {"name": "x"}}) for t in (3, 1))
    assert [m.t for m in parse_chat(lines)] == [1.0, 3.0]
    rechat = json.dumps([{"content_offset_seconds": 5, "message": {"body": "hi"}}])
    assert parse_chat(rechat)[0].text == "hi"
    txt = "[0:01:23] Bob: ахахаха\n[1:00:00] <Ann> Pog\nnot a chat line\n00:00:05 Kim: hello there"
    assert [(m.t, m.author, m.text) for m in parse_chat("\ufeff" + txt)] == [
        (5.0, "Kim", "hello there"), (83.0, "Bob", "ахахаха"), (3600.0, "Ann", "Pog")]
    assert parse_chat("just words") == []


def test_unreadable_chat_file(tmp_path):
    file = tmp_path / "x.txt"
    file.write_text("nothing here", encoding="utf-8")
    with pytest.raises(chat_mod.ChatError):
        chat_mod.read_chat_file(file)


@pytest.mark.parametrize("text, kind", [
    ("KEKW", CHAT_LAUGH), ("OMEGALUL OMEGALUL", CHAT_LAUGH), ("ахахах", CHAT_LAUGH), ("))))", CHAT_LAUGH),
    ("Pog", CHAT_HYPE), ("ВАУ", CHAT_HYPE), ("what???", CHAT_HYPE), ("привет", ""), ("+", ""),
])
def test_message_kinds(text, kind):
    assert message_kind(text) == kind


def test_vod_links():
    assert parse_vod_url("https://www.twitch.tv/videos/2283746123") == chat_mod.VodLink("2283746123", 0)
    assert parse_vod_url("twitch.tv/videos/2283746123?t=1h02m03s") == chat_mod.VodLink("2283746123", 3723)
    assert parse_vod_url("2283746123").video_id == "2283746123"
    assert parse_vod_url("https://clips.twitch.tv/FunnyClip") is None
    assert parse_vod_url("") is None


def test_chat_cache_round_trip(tmp_path):
    msgs = [ChatMessage(1.5, "KEKW", "Bob"), ChatMessage(2.0, "текст", "")]
    chat_mod.save_chat(msgs, "C:/rec.mp4", tmp_path, "file.json")
    assert chat_mod.load_chat("c:\\REC.mp4", tmp_path) == (msgs, "file.json")
    chat_mod.forget_chat("C:/rec.mp4", tmp_path)
    assert chat_mod.load_chat("C:/rec.mp4", tmp_path) is None
    assert chat_mod.cut(msgs, 1.0, 1.8) == [ChatMessage(0.5, "KEKW", "Bob")]


def test_chat_bursts_become_events():
    msgs = _chat(600, [(206, 40, "KEKW"), (406, 30, "Pog"), (306, 25, "что")])
    events = chat_events(msgs, 600 * S)
    times = {round(e.t_us / S): e.reason for e in events}
    # chat reacts ~6 s late: the events sit at the moment itself
    assert {200, 300, 400} <= {t - t % 100 + (100 if t % 100 > 90 else 0) for t in times}
    by_time = sorted(events, key=lambda e: e.t_us)
    assert [e.reason for e in by_time] == [CHAT_LAUGH, CHAT, CHAT_HYPE]
    assert all(195 * S <= e.t_us <= 205 * S for e in by_time[:1])
    assert chat_events(_chat(600, []), 600 * S) == []  # steady chat: nothing
    assert chat_events([], 600 * S) == []


def test_fetch_vod_chat_pages_and_errors():
    pages = [
        {"data": {"video": {"comments": {
            "edges": [{"cursor": "c1", "node": {"id": "1", "contentOffsetSeconds": 95,
                                                "commenter": {"displayName": "a"},
                                                "message": {"fragments": [{"text": "early"}]}}},
                      {"cursor": "c2", "node": {"id": "2", "contentOffsetSeconds": 110,
                                                "commenter": None, "message": {"fragments": [{"text": "KEKW"}]}}}],
            "pageInfo": {"hasNextPage": True}}}}},
        {"data": {"video": {"comments": {
            "edges": [{"cursor": "c3", "node": {"id": "3", "contentOffsetSeconds": 150,
                                                "commenter": {"displayName": "b"},
                                                "message": {"fragments": [{"text": "Pog"}]}}},
                      {"cursor": "c4", "node": {"id": "4", "contentOffsetSeconds": 400,
                                                "commenter": {"displayName": "c"},
                                                "message": {"fragments": [{"text": "late"}]}}}],
            "pageInfo": {"hasNextPage": True}}}}},
    ]
    sent = []

    def post(body):
        sent.append(body[0]["variables"])
        return [pages[len(sent) - 1]]

    progress = []
    msgs = chat_mod.fetch_vod_chat(chat_mod.VodLink("123456789", 100), 120, post=post,
                                   progress=lambda f, t: progress.append(f))
    assert [(m.t, m.text, m.author) for m in msgs] == [(10.0, "KEKW", ""), (50.0, "Pog", "b")]
    assert sent == [{"videoID": "123456789", "contentOffsetSeconds": 100},
                    {"videoID": "123456789", "cursor": "c2"}]  # stops after passing the end
    assert progress[-1] == 1.0

    with pytest.raises(chat_mod.ChatError, match="refused"):
        chat_mod.fetch_vod_chat(chat_mod.VodLink("1234567", 0), 60,
                                post=lambda b: [{"errors": [{"message": "failed integrity check"}]}])
    with pytest.raises(chat_mod.ChatError, match="not found"):
        chat_mod.fetch_vod_chat(chat_mod.VodLink("1234567", 0), 60, post=lambda b: [{"data": {"video": None}}])
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(chat_mod.ChatCancelled):
        chat_mod.fetch_vod_chat(chat_mod.VodLink("1234567", 0), 60, post=post, cancel=cancel)


# ------------------------------------------------------------------ windows
def test_defaults_by_length():
    assert default_length(60 * S) == AUTO and default_length(3600 * S) == 30
    assert default_limit(60 * S) == 5 and default_limit(3 * 3600 * S) == 30


def test_windows_prefer_mixed_evidence_and_do_not_overlap():
    events = ([Event(100 * S, 0.6, LOUD), Event(101 * S, 0.7, LAUGH), Event(103 * S, 0.8, CHAT_LAUGH)]
              + [Event(t * S, 0.5, LOUD) for t in range(300, 330, 3)]  # ten loud spikes, one kind, 27 s
              + [Event(500 * S, 0.4, "hype")])  # a lone hype word is not a moment
    windows = pick_windows(events, 600 * S, 30 * S, 10, 0.7)
    assert len(windows) == 2
    first, second = windows
    assert first[0] < 100 * S and first[1] > 103 * S  # setup before, reaction after
    assert first[2] > second[2]  # three kinds beat ten spikes of one kind
    assert all(e - s == 30 * S for s, e, _ in windows)
    assert pick_windows([], 600 * S, 30 * S, 10, 0.7) == []


def test_snap_to_sentences_and_quiet():
    bounds = ([88 * S, 95 * S, 130 * S], [94 * S, 118 * S, 140 * S])
    assert snap(90 * S, 120 * S, 600 * S, bounds) == (88 * S - 150_000, 118 * S + 300_000)
    # Without a transcript: the quietest point near the edge.
    curve = (np.arange(0, 600, 0.1), np.zeros(6000))
    curve[1][int(89.2 * 10)] = -60
    curve[1][int(119.5 * 10)] = -60
    s, e = snap(90 * S, 120 * S, 600 * S, ([], []), curve)
    assert abs(s - 89.2 * S) < 0.11 * S and abs(e - 119.5 * S) < 0.11 * S
    assert snap(90 * S, 120 * S, 600 * S, ([], [])) == (90 * S, 120 * S)


def test_quote_near_peak():
    t = _transcript([(10, 14, "ну давай"), (15, 18, "ахаха он упал прямо в лаву с разбега"), (19, 25, "ладно")])
    assert quote(t, 8 * S, 30 * S, 16 * S) == "ахаха он упал прямо в лаву с разбега"
    assert quote(t, 8 * S, 30 * S, 11 * S) == "ну давай ахаха он упал прямо в лаву с разбега"
    assert quote(None, 0, 1, 0) == ""


def test_find_moments_in_a_long_recording():
    duration = 900
    waveform = Waveform(_peaks(duration, 0.08, [(200.0, 201.5, 0.9), (610.0, 611.0, 0.85)]), PPS)
    transcript = _transcript([(185, 192, "смотри что сейчас будет"), (199, 203, "ахахаха жесть"),
                              (204, 212, "я не верю"), (600, 605, "окей"), (609, 612, "ДААААА")], duration)
    chat = _chat(duration, [(206, 40, "KEKW"), (450, 30, "Pog")])
    found = find_moments(waveform, transcript, duration * S, 30, chat=chat)
    assert len(found) >= 3
    best = found[0]
    assert best.start_us <= 199 * S and best.end_us >= 203 * S
    assert {LOUD, LAUGH, CHAT_LAUGH} <= set(best.reasons)
    assert best.score == max(h.score for h in found)
    assert best.chat_count >= 40 and "ахахаха" in best.quote
    assert best.start_us == 185 * S - 150_000  # starts on the sentence before (the setup)
    for h in found:
        assert 20 * S <= h.length_us <= 41 * S
    ordered = sorted(found, key=lambda h: h.start_us)
    assert all(a.end_us <= b.start_us for a, b in zip(ordered, ordered[1:]))
    # Without chat or transcript it still works from the sound alone.
    alone = find_moments(waveform, None, duration * S, 45)
    assert alone and all(h.reasons == (LOUD,) for h in alone)
    assert find_moments(None, None, duration * S, 30) == []


def test_review_ticks_survive_a_new_search_and_are_saved():
    old = [Highlight(10 * S, 40 * S, 20 * S, 0.8, (LOUD,), accepted=True), Highlight(100 * S, 130 * S, 110 * S, 0.5, (LOUD,))]
    new = [Highlight(12 * S, 42 * S, 20 * S, 0.8, (LOUD,)), Highlight(101 * S, 131 * S, 110 * S, 0.5, (LOUD,))]
    merged = merge_review(old, new)
    assert [h.accepted for h in merged] == [True, False]
    h = Highlight(1 * S, 31 * S, 3 * S, 0.8, (LOUD, CHAT), "фраза", True, 12)
    project = Project()
    project.set_highlights("C:/rec.mp4", [h])
    assert Project.from_dict(project.to_dict()).highlights_for("C:/rec.mp4") == (h,)
    assert h.label == "Loud, Chat burst"


# ------------------------------------------------------------------ clips
def test_clip_names_are_safe():
    h = Highlight(3723 * S, 3753 * S, 3730 * S, 0.9, (LOUD,), 'он сказал: "что?" / прямо в лаву и долго падал вниз…')
    name = clip_name(Path("C:/rec/Stream 2026.mp4"), h, 3)
    assert name.startswith("Stream 2026 – 03 – 1-02-03 – он сказал") and name.endswith(".mp4")
    assert not any(c in name for c in '<>:"/\\|?*')
    assert clip_name(Path("x.mp4"), Highlight(0, S, 0, 0.5, ()), 1) == "x – 01 – 0-00-00.mp4"


def test_slice_transcript():
    t = _transcript([(10, 14, "раз два три четыре"), (20, 24, "пять шесть"), (40, 44, "семь")])
    part = slice_transcript(t, 12 * S, 30 * S)
    assert part.duration_us == 18 * S
    assert [s.text for s in part.segments] == ["три четыре", "пять шесть"]
    assert part.segments[0].start_us == 0 and part.segments[1].start_us == 8 * S
    assert part.segments[0].words[0].start_us == 0 and [s.id for s in part.segments] == [1, 2]
    assert slice_transcript(None, 0, S) is None


@pytest.fixture(scope="module")
def long_clip(tmp_path_factory, ffmpeg_paths) -> Path:
    """40 s of quiet 'talk' with loud bursts at 10-11 s and 30-31 s."""
    path = tmp_path_factory.mktemp("mom") / "stream.mp4"
    subprocess.run([
        str(ffmpeg_paths.ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=40",
        "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=48000:duration=40",
        "-filter_complex", "[1:a]volume='if(between(t,10,11)+between(t,30,31),0.9,0.03)':eval=frame[a]",
        "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path),
    ], check=True)
    return path


def test_save_clips_cuts_exact_ranges(tmp_path, ffmpeg_paths, long_clip):
    from core.media.probe import probe_media

    moments = [Highlight(28 * S, 34 * S, 30 * S, 0.9, (LOUD,), "второй"),
               Highlight(5 * S, 13 * S, 10 * S, 0.8, (LOUD,), "первый")]
    progress = []
    saved = save_clips(ffmpeg_paths.ffmpeg, long_clip, moments, tmp_path / "out",
                       progress=lambda f, t: progress.append(f))
    assert [p.name for p, _ in saved] == ["stream – 01 – 0-00-05 – первый.mp4", "stream – 02 – 0-00-28 – второй.mp4"]
    info = probe_media(saved[0][0], ffmpeg_paths.ffprobe)
    assert info.duration_s == pytest.approx(8.0, abs=0.15) and info.has_audio
    assert progress[-1] == 1.0
    assert not list((tmp_path / "out").glob("*.part.mp4"))
    # Saved before: reused, not cut again.
    again = save_clips(ffmpeg_paths.ffmpeg, long_clip, moments, tmp_path / "out",
                       run=lambda *a: pytest.fail("should reuse"))
    assert [p for p, _ in again] == [p for p, _ in saved]
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ClipCancelled):
        save_clips(ffmpeg_paths.ffmpeg, long_clip, moments, tmp_path / "other", cancel=cancel)


# ------------------------------------------------------------------ window
def test_window_long_moments_chat_ticks_and_clips(qapp, data_dir, ffmpeg_paths, long_clip, tmp_path):
    from PySide6.QtCore import Qt

    from app.config import AppConfig
    from tests.test_ui import _wait
    from ui.main_window.main_window import MainWindow

    w = MainWindow(config=AppConfig.load(data_dir), ffmpeg=ffmpeg_paths)
    w.import_files([str(long_clip)])
    assert _wait(lambda: w.timeline.timeline is not None)
    panel, ctl = w.highlights_panel, w.highlights
    assert panel.length_s == AUTO  # short clip: short moments by default
    assert "No chat" in panel.chat_label.text() and not panel.save_button.isEnabled()

    # A transcript so the new clips get their part of it.
    t = _transcript([(8, 12, "ой ой ой"), (29, 32, "ахахаха")], 40)
    w.project.set_transcript(str(long_clip), t)

    chat_file = tmp_path / "chat.txt"
    chat_file.write_text("\n".join(f"[0:00:{s:02d}] u{s}: KEKW" for s in range(15, 19))
                         + "\n[0:00:01] a: hi\n[0:00:25] b: hi", encoding="utf-8")
    assert ctl.load_chat_file(chat_file)
    assert _wait(lambda: ctl.chat, 10) and "6 messages" in panel.chat_label.text()
    assert panel.chat_clear_button.isEnabled()

    panel.set_length(20)
    panel.find_button.click()
    assert _wait(lambda: panel.list.count() > 0, 20)
    assert all(15 * S <= h.length_us <= 27 * S for h in ctl.highlights)
    item = panel.list.item(0)
    index = item.data(Qt.ItemDataRole.UserRole + 1)
    item.setCheckState(Qt.CheckState.Checked)
    assert ctl.highlights[index].accepted and w.project.highlights_for(str(long_clip))[index].accepted
    assert panel.save_button.isEnabled() and "1 ticked" in panel.ticked_label.text()
    assert w.timeline_panel.view._highlights[index].accepted

    # Searching again keeps the tick.
    panel.find_button.click()
    assert _wait(lambda: panel.find_button.text() == "Find highlights", 20)
    assert any(h.accepted for h in ctl.highlights)

    out = tmp_path / "clips"
    assert ctl.save_clips(out)
    assert _wait(lambda: w.media_panel.list.count() >= 2 if hasattr(w.media_panel, "list") else
                 len(w.project.media) >= 2, 60)
    files = list(out.glob("*.mp4"))
    assert len(files) == 1
    assert w.project.transcript_for(str(files[0])) is not None
    assert chat_mod.load_chat(files[0], w.config.cache_dir) is not None
    assert panel.save_button.text() == "Save ticked as clips…"

    panel.chat_clear_button.click()
    assert ctl.chat == [] and chat_mod.load_chat(w.preview.media.path, w.config.cache_dir) is None
    w._set_dirty(False)
    w.close()
