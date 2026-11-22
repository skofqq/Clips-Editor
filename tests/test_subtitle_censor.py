"""Subtitle censoring as an option (all swear words or only muted ones) and emoji switched off."""

from __future__ import annotations

from dataclasses import replace

from core.censor.dictionary import Dictionary
from core.subtitles.model import (
    CENSOR_ALL,
    CENSOR_MUTED,
    MASK_ALL,
    MASK_FIRST,
    MASK_NONE,
    PRESETS,
    SubtitleSettings,
    SubtitleStyle,
    build_captions,
    style_from_preset,
)
from core.timeline.model import MuteRegion, Timeline
from core.transcription.model import Segment, Transcript, Word

S = 1_000_000


def _transcript() -> Transcript:
    words = (Word(0, 400_000, "это"), Word(500_000, 900_000, "пиздец"), Word(S, int(1.4 * S), "бро"),
             Word(int(1.5 * S), int(1.9 * S), "капибара"))
    return Transcript("ru", 0.9, "small", (Segment(0, 0, 2 * S, "это пиздец бро капибара", words),), 3 * S)


def _text(style, timeline=None, dictionary=None) -> str:
    timeline = timeline or Timeline.for_source(3 * S)
    caps = build_captions(_transcript(), timeline, replace(style, max_words=8, uppercase=False),
                          dictionary=dictionary)
    return " ".join(c.text for c in caps)


def test_scope_and_off_switch():
    muted = replace(Timeline.for_source(3 * S), mutes=(MuteRegion(1, 500_000, 900_000),))
    base = SubtitleStyle(enabled=True)
    assert base.censor_scope == CENSOR_MUTED and base.mask == MASK_FIRST  # old projects unchanged
    assert _text(base) == "это пиздец бро капибара"  # nothing muted: written as spoken
    assert _text(base, muted) == "это п***** бро капибара"
    every = replace(base, censor_scope=CENSOR_ALL)
    assert _text(every) == "это п***** бро капибара"  # found by the dictionary, sound not muted
    assert _text(replace(every, mask=MASK_ALL)) == "это ****** бро капибара"
    assert _text(replace(every, mask=MASK_NONE), muted) == "это пиздец бро капибара"  # censoring off
    assert _text(replace(base, mask=MASK_NONE), muted) == "это пиздец бро капибара"
    # The user's own dictionary words count too.
    mine = Dictionary(custom_words=["капибара"])
    assert _text(every, dictionary=mine) == "это п***** бро к*******"
    assert SubtitleStyle.from_dict(replace(every, mask=MASK_ALL).to_dict()).censor_scope == CENSOR_ALL
    assert SubtitleStyle.from_dict({"censor_scope": "some"}).censor_scope == CENSOR_MUTED


def test_presets_keep_censoring_and_emoji_off():
    current = SubtitleStyle(mask=MASK_NONE, censor_scope=CENSOR_ALL)
    style = style_from_preset(PRESETS["Karaoke sweep"], current)
    assert (style.mask, style.censor_scope, style.emoji) == (MASK_NONE, CENSOR_ALL, True)
    assert not style_from_preset(PRESETS["Karaoke sweep"], current, emoji_allowed=False).emoji


def test_panel_censor_and_emoji_switch(qapp):
    from ui.subtitles.subtitles_panel import SubtitlesPanel

    panel = SubtitlesPanel()
    seen, allowed = [], []
    panel.settings_changed.connect(seen.append)
    panel.emoji_allowed_changed.connect(allowed.append)
    panel.set_settings(SubtitleSettings(SubtitleStyle(enabled=True, mask=MASK_ALL, emoji=True)))
    assert panel.censor_check.isChecked() and panel.mask_combo.currentData() == MASK_ALL
    panel.censor_check.setChecked(False)
    assert seen[-1].style.mask == MASK_NONE and not panel.mask_combo.isEnabled()
    panel.censor_check.setChecked(True)
    assert seen[-1].style.mask == MASK_ALL  # the last way of writing comes back
    panel.censor_scope_combo.setCurrentIndex(panel.censor_scope_combo.findData(CENSOR_ALL))
    assert seen[-1].style.censor_scope == CENSOR_ALL

    panel.emoji_check.click()  # the user switches emoji off
    assert allowed == [False] and not seen[-1].style.emoji and not panel.emoji_allowed()
    panel.preset_list.itemClicked.emit(panel.preset_item("Karaoke sweep"))
    assert not panel.style().emoji and not panel.emoji_check.isChecked()  # styles bring no emoji now
    assert panel.style().censor_scope == CENSOR_ALL and panel.style().mask == MASK_ALL
    panel.emoji_check.click()
    assert allowed == [False, True] and panel.style().emoji
    panel.set_emoji_allowed(False)
    assert not panel.emoji_allowed()
    panel.deleteLater()


def test_config_keeps_the_emoji_switch(data_dir):
    from app.config import AppConfig

    config = AppConfig.load(data_dir)
    assert config.subtitle_emoji
    config.subtitle_emoji = False
    config.save()
    assert AppConfig.load(data_dir).subtitle_emoji is False
