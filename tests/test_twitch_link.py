"""0.9.36: fill the Data page from a Twitch link, clip title ideas from the transcript."""

from __future__ import annotations

import urllib.error

import numpy as np
import pytest

from core.audio.waveform import Waveform
from core.censor.dictionary import Dictionary
from core.metadata import twitch_link as tl
from core.metadata.generate import ClipMetadata, MetadataSelection
from core.metadata.library import GAME, PARTICIPANT, STREAMER, TEMPLATE, Library
from core.metadata.said_titles import format_title, title_ideas, waveform_loudness
from core.transcription.model import Segment, Transcript, Word


@pytest.fixture
def lib(tmp_path):
    library = Library(tmp_path / "lib.sqlite")
    yield library
    library.close()


# ------------------------------------------------------------------ links
@pytest.mark.parametrize("text, kind, ident, login, offset", [
    ("https://clips.twitch.tv/FunnyClipSlug-AbC_12", tl.CLIP, "FunnyClipSlug-AbC_12", "", 0),
    ("https://www.twitch.tv/Sasavot/clip/SmartTurtle-x9?filter=clips", tl.CLIP, "SmartTurtle-x9", "sasavot", 0),
    ("https://m.twitch.tv/clip/LazySlug", tl.CLIP, "LazySlug", "", 0),
    ("https://clips.twitch.tv/embed?clip=EmbedSlug&parent=x", tl.CLIP, "EmbedSlug", "", 0),
    ("https://www.twitch.tv/videos/2012345678?t=1h2m3s", tl.VIDEO, "2012345678", "", 3723),
    ("twitch.tv/videos/2012345678", tl.VIDEO, "2012345678", "", 0),
    ("https://www.twitch.tv/Sasavot", tl.CHANNEL, "sasavot", "sasavot", 0),
])
def test_parse_link(text, kind, ident, login, offset):
    link = tl.parse_link(text)
    assert link == tl.TwitchLink(kind, ident, login, offset)


@pytest.mark.parametrize("text", ["", "hello", "https://youtube.com/watch?v=1", "https://www.twitch.tv/directory"])
def test_not_a_twitch_link(text):
    assert tl.parse_link(text) is None


def _clip_answer(title="Саша упала с крыши @Mira_Live", stream_title="ГТА РП с друзьями !tg"):
    return {"data": {"clip": {
        "title": title, "createdAt": "2026-11-20T18:30:00Z", "videoOffsetSeconds": 3723,
        "broadcaster": {"login": "sasavot", "displayName": "Sasavot"},
        "game": {"name": "Grand Theft Auto V", "displayName": "Grand Theft Auto V"},
        "video": {"id": "2012345678", "title": stream_title, "createdAt": "2026-11-20T16:00:00Z"},
    }}}


def _post(answers):
    calls = []

    def post(body):
        calls.append(body)
        query = body["query"]
        for key, answer in answers.items():
            if key in query:
                if isinstance(answer, Exception):
                    raise answer
                return answer
        return {"data": {}}
    post.calls = calls
    return post


def test_fetch_clip_info():
    post = _post({"clip(": _clip_answer(), "users(": {"data": {"users": [{"login": "mira_live",
                                                                          "displayName": "Mira_Live"}]}}})
    info = tl.fetch_info("https://clips.twitch.tv/Slug", post)
    assert info.streamer_login == "sasavot" and info.streamer_name == "Sasavot"
    assert info.game == "Grand Theft Auto V"
    assert info.clip_title == "Саша упала с крыши Mira_Live"
    assert info.stream_url == "https://www.twitch.tv/videos/2012345678?t=1h2m3s"
    assert info.mentions == (("mira_live", "Mira_Live"),)
    assert len(info.date) == 10 and info.date.endswith(".11.2026")
    assert post.calls[0]["variables"] == {"slug": "Slug"}


def test_clip_with_the_streams_title_gives_no_clip_title(lib):
    post = _post({"clip(": _clip_answer(title="ГТА РП с друзьями !tg")})
    info = tl.fetch_info("https://clips.twitch.tv/Slug", post)
    assert info.clip_title == ""
    result = tl.apply_info(ClipMetadata(MetadataSelection(clip_title="Мой заголовок")), info, lib)
    assert result.metadata.selection.clip_title == "Мой заголовок"
    assert any("stream's title" in n for n in result.notes)


def test_video_and_channel_info():
    video = {"data": {"video": {"id": "1", "title": "Стрим с @Lexa", "createdAt": "2026-11-01T10:00:00Z",
                                "owner": {"login": "sasavot", "displayName": "Sasavot"},
                                "game": {"name": "Just Chatting", "displayName": "Общение"}}}}
    info = tl.fetch_info("https://www.twitch.tv/videos/2012345678?t=10m", _post({"video(": video}))
    assert info.game == "Общение" and info.clip_title == "" and info.stream_title == "Стрим с @Lexa"
    assert info.stream_url == "https://www.twitch.tv/videos/2012345678?t=10m0s"
    assert info.mentions == (("lexa", "lexa"),)  # names not given: the login is used
    user = {"data": {"user": {"login": "sasavot", "displayName": "Sasavot",
                              "broadcastSettings": {"title": "x", "game": {"name": "Dota 2"}}}}}
    info = tl.fetch_info("https://twitch.tv/sasavot", _post({"user(": user}))
    assert info.game == "Dota 2" and info.stream_url == "https://www.twitch.tv/sasavot" and info.date == ""


def test_missing_clip_and_offline():
    with pytest.raises(tl.TwitchLinkError):
        tl.fetch_info("https://clips.twitch.tv/Gone", _post({"clip(": {"data": {"clip": None}}}))
    with pytest.raises(tl.TwitchLinkError):
        tl.fetch_info("not a link", _post({}))
    down = _post({"clip(": urllib.error.URLError("offline")})
    with pytest.raises(tl.TwitchLinkError):
        tl.fetch_info("https://clips.twitch.tv/Slug", down)  # nothing in the link itself
    info = tl.fetch_info("https://www.twitch.tv/sasavot/clip/Slug", down)
    assert not info.online and info.streamer_login == "sasavot" and info.stream_url == "https://www.twitch.tv/sasavot"
    refused = _post({"video(": {"errors": [{"message": "service error"}]}})
    info = tl.fetch_info("https://www.twitch.tv/videos/2012345678", refused)
    assert not info.online and info.stream_url == "https://www.twitch.tv/videos/2012345678"


def test_apply_info_uses_and_grows_the_library(lib):
    sasa = lib.create(STREAMER, "Саша", username="sasavot")
    lib.save(lib.create(TEMPLATE, "Main", title="{streamer}: {clip_title}", description="{game} {stream_url}",
                        is_default=True))
    old_friend = lib.create(PARTICIPANT, "Old friend")
    post = _post({"clip(": _clip_answer(), "users(": {"data": {"users": []}}})
    info = tl.fetch_info("https://clips.twitch.tv/Slug", post)
    current = ClipMetadata(MetadataSelection(participant_ids=(old_friend.id,)))
    result = tl.apply_info(current, info, lib)
    s = result.metadata.selection
    assert s.streamer_id == sasa.id  # found by the login, not added again
    mira = lib.find(PARTICIPANT, "mira_live")
    assert mira is not None and mira.twitch_url == "https://www.twitch.tv/mira_live"
    assert s.participant_ids == (old_friend.id, mira.id)
    game = lib.find(GAME, "Grand Theft Auto V")
    assert game is not None and s.game_id == game.id
    assert "mira_live" in result.added and "Sasavot" not in result.added
    assert result.metadata.title == "Саша: Саша упала с крыши Mira_Live"
    assert "GTA V https://www.twitch.tv/videos/2012345678?t=1h2m3s" in result.metadata.description
    assert {"streamer", "game", "clip title", "date", "stream link", "participants"} <= set(result.filled)
    # Twice: nothing new in the library.
    again = tl.apply_info(result.metadata, info, lib)
    assert again.added == () and again.metadata.selection == s


def test_apply_info_keeps_hand_edits(lib):
    lib.save(lib.create(TEMPLATE, "Main", title="{clip_title}", is_default=True))
    info = tl.TwitchInfo(tl.TwitchLink(tl.CLIP, "x"), "sasavot", "Sasavot", clip_title="Новое")
    first = tl.apply_info(ClipMetadata(), info, lib).metadata
    edited = ClipMetadata(first.selection, first.generated, "Мой собственный заголовок", first.description, first.tags)
    result = tl.apply_info(edited, tl.TwitchInfo(tl.TwitchLink(tl.CLIP, "y"), "sasavot", "Sasavot",
                                                 clip_title="Другое"), lib)
    assert result.metadata.title == "Мой собственный заголовок"
    assert result.metadata.selection.clip_title == "Другое"


# ------------------------------------------------------------------ title ideas
def _transcript(lines):
    """lines: (start_s, text) — words spread 0.3 s apart."""
    segments = []
    for i, (start, text) in enumerate(lines):
        words = []
        t = int(start * 1e6)
        for token in text.split():
            words.append(Word(t, t + 250_000, token))
            t += 300_000
        segments.append(Segment(i, words[0].start_us, words[-1].end_us, text, tuple(words)))
    return Transcript("ru", 1.0, "test", tuple(segments), 0)


def test_title_ideas_pick_the_striking_phrases():
    transcript = _transcript([
        (0, "ну вот сейчас мы пойдём в магазин и купим что-нибудь поесть."),
        (5, "Нет нет нет, это жесть!"),
        (8, "ты зачем туда прыгнул?"),
        (11, "эээ ну типа да"),
        (14, "Ахахах я не могу."),
        (17, "короче ладно."),
    ])
    ideas = title_ideas(transcript)
    texts = [i.text for i in ideas]
    assert 3 <= len(ideas) <= 5
    assert texts[0] == "Нет нет нет, это жесть!"
    assert "Ты зачем туда прыгнул?" in texts
    assert all("эээ" not in t.casefold() and not t.casefold().startswith("ну ") for t in texts)
    assert "Короче ладно" not in texts and all(len(t) <= 70 for t in texts)
    kinds = {i.text: i.kind for i in ideas}
    assert kinds["Ты зачем туда прыгнул?"] == "question"


def test_title_ideas_leave_out_swears_and_use_loudness():
    transcript = _transcript([
        (0, "это просто пиздец какой-то!"),
        (4, "он едет по дороге домой"),
        (8, "он сейчас нас догонит"),
        (12, "давай быстрее уходим отсюда"),
    ])
    peaks = np.full(16 * 100, 40, dtype=np.uint8)
    peaks[1200:1400] = 250  # the last phrase is shouted
    loud = waveform_loudness(Waveform(peaks, 100))
    ideas = title_ideas(transcript, Dictionary(), loud)
    texts = [i.text for i in ideas]
    assert all("пизд" not in t for t in texts)
    assert texts[0] == "Давай быстрее уходим отсюда"
    assert ideas[0].kind == "loud"
    assert title_ideas(None) == [] and waveform_loudness(None) is None


def test_long_phrase_is_cut_and_format_title():
    long = " ".join(["слово"] * 3 + ["вдруг", "он", "взял", "и", "побежал", "прямо", "в", "стену", "дома"])
    ideas = title_ideas(_transcript([(0, long + ".")]))
    assert ideas and ideas[0].text.endswith("…") and len(ideas[0].text.split()) <= 10
    assert format_title(["ну,", "что", "это", "было?!"], "?") == "Ну, что это было?"
    assert format_title(["это", "жесть,"], "") == "Это жесть"


def test_pauses_split_phrases():
    words = (Word(0, 200_000, "смотри"), Word(300_000, 500_000, "сюда"),
             Word(2_000_000, 2_200_000, "вот"), Word(2_300_000, 2_500_000, "это"),
             Word(2_600_000, 2_800_000, "прыжок!"))
    t = Transcript("ru", 1.0, "t", (Segment(0, 0, 2_800_000, "смотри сюда вот это прыжок!", words),), 0)
    texts = [i.text for i in title_ideas(t)]
    assert "Это прыжок!" in texts and "Смотри сюда" in texts


# ------------------------------------------------------------------ panel
def test_panel_title_idea_and_link_fill_with_undo(qapp, lib):
    from core.metadata.said_titles import TitleIdea
    from ui.metadata.metadata_panel import MetadataPanel

    lib.save(lib.create(TEMPLATE, "Main", title="{streamer}: {clip_title}", is_default=True))
    panel = MetadataPanel(lib)
    seen = []
    panel.metadata_changed.connect(seen.append)
    assert panel.ideas_box.isHidden()
    ideas = [TitleIdea("Это жесть!", "shout", 0, 1, 5.0), TitleIdea("Ты куда?", "question", 2, 3, 4.0)]
    panel.set_title_ideas(ideas)
    assert not panel.ideas_box.isHidden() and panel.idea_texts() == ["Это жесть!", "Ты куда?"]
    panel.idea_buttons[1].click()
    meta = panel.metadata()
    assert meta.selection.clip_title == "Ты куда?" and meta.title == "Ты куда?"
    assert panel.idea_buttons[1].isChecked() and not panel.idea_buttons[0].isChecked()
    assert seen[-1] == meta

    requested = []
    panel.twitch_link_requested.connect(requested.append)
    panel.link_edit.setText("https://clips.twitch.tv/Slug")
    panel.link_button.click()
    assert requested == ["https://clips.twitch.tv/Slug"]
    before = panel.metadata()
    info = tl.TwitchInfo(tl.TwitchLink(tl.CLIP, "Slug"), "sasavot", "Sasavot", game="Dota 2", date="20.11.2026",
                         stream_url="https://www.twitch.tv/sasavot")
    result = tl.apply_info(before, info, lib)
    panel.refresh_library()
    panel.show_filled(before, result.metadata, "Filled.")
    assert panel.metadata().title == "Sasavot: Ты куда?" and panel.date_edit.text() == "20.11.2026"
    assert panel.game_chips.labels() == ("Dota 2",) or list(panel.game_chips.labels()) == ["Dota 2"]
    panel.undo_carry_over()
    assert panel.metadata().selection == before.selection


def test_window_fills_from_link_and_shows_ideas(qapp, data_dir, ffmpeg_paths, monkeypatch):
    from tests.test_steps import _wait, _window

    w = _window(data_dir, ffmpeg_paths)
    try:
        answer = _clip_answer()
        monkeypatch.setattr(tl, "_gql_post", lambda body, timeout=20.0: answer if "clip(" in body["query"]
                            else {"data": {"users": []}})
        w.metadata_panel.link_edit.setText("https://clips.twitch.tv/Slug")
        w.metadata_panel.link_button.click()
        assert _wait(lambda: w.twitch_link._task is None and w.metadata_panel.date_edit.text())
        meta = w.metadata_panel.metadata()
        assert w.metadata_library.find(STREAMER, "sasavot").id == meta.selection.streamer_id
        assert meta.selection.clip_title == "Саша упала с крыши Mira_Live"
        assert not w.metadata_panel.carry_row.isHidden()
        assert "Twitch clip" in w.metadata_panel.carry_label.text()
        assert w.metadata_panel.link_button.isEnabled()

        w.transcript.set_transcript(_transcript([(0, "Нет нет нет, это жесть!"), (3, "ты куда прыгнул?")]))
        assert _wait(lambda: len(w.metadata_panel.idea_texts()) == 2)
        w.transcript.set_transcript(None)
        assert _wait(lambda: w.metadata_panel.idea_texts() == [])
    finally:
        w._set_dirty(False)
        w.close()
