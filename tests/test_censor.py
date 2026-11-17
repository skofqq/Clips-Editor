"""Phase 5: profanity dictionaries, detection and review."""

from __future__ import annotations

import pytest

from core.censor.dictionary import CUSTOM, MILD, STRONG, Dictionary
from core.censor.normalize import canonical, forms, transliterate
from core.censor.review import (
    ACCEPTED,
    PENDING,
    REJECTED,
    CensorSettings,
    CensorState,
    detect,
    merge_ranges,
)
from core.timeline.model import BEEP, SILENCE, Timeline
from core.transcription.model import Segment, Transcript, Word

ALL = (STRONG, MILD, CUSTOM)

PROFANE = [
    "пиздец", "Пиздёж", "бляяять", "блять", "бля", "cyка", "сука", "сучка", "сукин", "blyat", "nahuy", "хуй",
    "охуеть", "похуй", "нахуя", "хуйня", "ебать", "ёбаный", "заебал", "наебал", "выебываться", "ебучий",
    "пидорас", "мудак", "п1зд3ц", "fuck", "Fucking", "fuuuck", "motherfucker", "shit", "bullshit", "bitch",
    "б**дь", "f***",
]
CLEAN = [
    "хлеба", "требую", "колебаться", "сукно", "херсон", "мандарин", "застрахуй", "небо", "себя", "учебник",
    "ребята", "потребуется", "любой", "рубль", "глубокий", "употреблять", "скребу", "блин", "бляха", "хулиган",
    "учёба", "class", "assistant", "hello", "glass", "Scunthorpe", "shiitake", "cocktail", "катка", "кринж",
]


@pytest.mark.parametrize("word", PROFANE)
def test_profanity_matches(word):
    assert Dictionary().match(word, ALL) is not None, word


@pytest.mark.parametrize("word", CLEAN)
def test_clean_words_do_not_match(word):
    assert Dictionary().match(word, ALL) is None, word


def test_categories():
    d = Dictionary()
    assert d.match("жопа") is None  # mild is off by default
    assert d.match("жопа", (MILD,)).category == MILD
    assert d.match("damn", (STRONG, MILD)).language == "en"
    assert d.match("сука").category == STRONG


def test_normalisation():
    assert transliterate("blyat") == "блят"
    assert transliterate("nahuy") == "нахуй"
    assert "сука" in forms("cyкa")
    assert canonical("СУУУКА!") == "сука"
    assert forms("...") == []


def test_custom_words_and_exclusions(tmp_path):
    d = Dictionary(custom_words=["кринж", "рофл*", "*ваш*", "# comment", ""])
    assert d.match("кринж").category == CUSTOM
    assert d.match("кринжово") is None  # exact entry
    assert d.match("рофлит").entry_key == "custom:рофл*"
    assert d.match("неваше") is not None
    assert d.add_exclusion("Сука")
    assert not d.add_exclusion("сука")  # already there
    assert d.match("сука") is None and d.match("СУУКА") is None
    assert d.match("суки") is not None  # other forms still caught
    path = tmp_path / "dict.json"
    d.save(path)
    loaded = Dictionary.load(path)
    assert loaded.custom_words == d.custom_words and loaded.exclusions == ["Сука"]
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    assert Dictionary.load(tmp_path / "bad.json").custom_words == []
    assert Dictionary.load(tmp_path / "missing.json").exclusions == []


# ------------------------------------------------------------- detection
def _transcript() -> Transcript:
    words = (
        Word(1_000_000, 1_300_000, "ну"),
        Word(1_300_000, 1_700_000, "бля,"),
        Word(1_700_000, 2_000_000, "это"),
        Word(2_100_000, 2_500_000, "пиздец!"),
    )
    second = (
        Word(5_000_000, 5_400_000, "what"),
        Word(5_400_000, 5_800_000, "the"),
        Word(5_800_000, 6_200_000, "fuck", timing="segment"),
        Word(6_200_000, 6_600_000, "damn"),
    )
    return Transcript(
        "ru", 0.9, "small",
        (Segment(0, 1_000_000, 2_500_000, "ну бля, это пиздец!", words),
         Segment(1, 5_000_000, 6_600_000, "what the fuck damn", second)),
        duration_us=10_000_000,
    )


def test_detect_creates_pending_suggestions_with_padding():
    state = detect(_transcript(), Dictionary())
    assert [s.text for s in state.suggestions] == ["бля,", "пиздец!", "fuck"]
    assert all(s.status == PENDING for s in state.suggestions)
    first = state.suggestions[0]
    assert (first.start_us, first.end_us) == (1_300_000 - 150_000, 1_700_000 + 250_000)
    assert first.exact and not state.suggestions[2].exact
    assert state.suggestions[2].language == "en"
    assert detect(_transcript(), Dictionary(), CensorSettings(categories=ALL)).count(PENDING) == 4
    assert detect(None, Dictionary()).suggestions == ()


def test_nothing_is_muted_without_acceptance():
    state = detect(_transcript(), Dictionary())
    timeline = Timeline.for_source(10_000_000)
    applied, state2 = state.apply(timeline)
    assert applied.mutes == () and state2.applied_mute_ids == ()
    rejected, _ = state.with_all_status(REJECTED).apply(timeline)
    assert rejected.mutes == ()


def test_accept_apply_merges_and_reapply_replaces():
    state = detect(_transcript(), Dictionary(), CensorSettings(pad_before_us=150_000, pad_after_us=400_000))
    ids = [s.id for s in state.suggestions]
    state = state.with_status(ids[:2], ACCEPTED)  # бля + пиздец overlap after padding
    assert merge_ranges([(0, 5), (5, 8), (10, 12)]) == [(0, 8), (10, 12)]
    assert state.accepted_ranges() == [(1_150_000, 2_900_000)]
    timeline = Timeline.for_source(10_000_000).add_mute(8_000_000, 9_000_000)  # hand-made mute
    timeline, state = state.apply(timeline)
    assert len(timeline.mutes) == 2 and len(state.applied_mute_ids) == 1
    assert state.is_applied(timeline)
    # Reject one and re-apply: the censor's mute is replaced, the user's kept.
    state = state.with_status([ids[1]], REJECTED)
    assert not state.is_applied(timeline)
    timeline, state = state.apply(timeline)
    spans = sorted((m.start_us, m.end_us) for m in timeline.mutes)
    assert spans == [(1_150_000, 2_100_000), (8_000_000, 9_000_000)]
    # Beep mode applies to the censor's regions.
    state = state.with_settings(CensorSettings(400_000 - 250_000, 400_000, BEEP))
    timeline, state = state.apply(timeline)
    assert {m.mode for m in timeline.mutes if m.id in state.applied_mute_ids} == {BEEP}
    assert {m.mode for m in timeline.mutes if m.id not in state.applied_mute_ids} == {SILENCE}
    timeline, state = state.remove_applied(timeline)
    assert [(m.start_us, m.end_us) for m in timeline.mutes] == [(8_000_000, 9_000_000)]


def test_adjust_range_and_padding_changes():
    state = detect(_transcript(), Dictionary())
    sid = state.suggestions[0].id
    state = state.with_range(sid, start_us=1_250_000, end_us=1_650_000, duration_us=10_000_000)
    s = state.get(sid)
    assert (s.start_us, s.end_us, s.adjusted) == (1_250_000, 1_650_000, True)
    state = state.with_range(sid, end_us=1_000_000)  # too short: kept at the minimum length
    assert state.get(sid).end_us - state.get(sid).start_us >= 20_000
    wider = state.with_settings(CensorSettings(pad_before_us=0, pad_after_us=0))
    assert wider.get(sid) == state.get(sid)  # hand-adjusted range kept
    other = wider.suggestions[1]
    assert (other.start_us, other.end_us) == (other.word_start_us, other.word_end_us)


def test_redetect_keeps_review_decisions():
    state = detect(_transcript(), Dictionary())
    first, second = state.suggestions[0].id, state.suggestions[1].id
    state = state.with_status([first], ACCEPTED).with_status([second], REJECTED)
    state = state.with_range(first, start_us=1_200_000)
    again = detect(_transcript(), Dictionary(custom_words=["это"]), previous=state)
    assert again.get(first).status == ACCEPTED and again.get(first).start_us == 1_200_000
    assert again.get(second).status == REJECTED
    new = [s for s in again.suggestions if s.text == "это"]
    assert new and new[0].status == PENDING and new[0].id not in (first, second)
    # Excluding a word drops its suggestion.
    d = Dictionary()
    d.add_exclusion("пиздец")
    assert "пиздец!" not in [s.text for s in detect(_transcript(), d, previous=state).suggestions]


def test_state_roundtrip():
    state = detect(_transcript(), Dictionary()).with_all_status(ACCEPTED)
    _, state = state.apply(Timeline.for_source(10_000_000))
    loaded = CensorState.from_dict(state.to_dict())
    assert loaded == state
    with pytest.raises(ValueError):
        CensorState.from_dict([])


def test_project_stores_censor_state(tmp_path):
    from core.projects.project import Project, load_project, save_project

    state = detect(_transcript(), Dictionary()).with_all_status(ACCEPTED)
    project = Project()
    project.set_censor("C:/clips/a.mp4", state)
    project.set_censor("C:/clips/b.mp4", CensorState())  # empty: not stored
    path = save_project(project, tmp_path / "p.tssproj")
    loaded = load_project(path)
    assert loaded.censor_for("c:/clips/A.mp4") == state
    assert loaded.censor_for("C:/clips/b.mp4") is None
    loaded.remove_media("C:/clips/a.mp4")
    assert loaded.censor_for("C:/clips/a.mp4") is None


# ------------------------------------------------------------------ UI
def _wait(predicate, timeout=10.0):
    import time

    from PySide6.QtWidgets import QApplication

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _clip_transcript() -> Transcript:
    words = (
        Word(100_000, 300_000, "ну"),
        Word(300_000, 600_000, "блять"),
        Word(700_000, 1_000_000, "это"),
        Word(1_100_000, 1_500_000, "пиздец"),
    )
    return Transcript("ru", 0.9, "small", (Segment(0, 100_000, 1_500_000, "ну блять это пиздец", words),),
                      duration_us=2_000_000)


@pytest.fixture
def window(qapp, data_dir, ffmpeg_paths, sample_clips):
    from app.config import AppConfig
    from ui.main_window.main_window import MainWindow

    w = MainWindow(config=AppConfig.load(data_dir), ffmpeg=ffmpeg_paths)
    w.resize(1400, 900)
    w.show()
    w.import_files([str(sample_clips["mp4"])])
    assert _wait(lambda: w.timeline.timeline is not None)
    yield w
    w._dirty = False
    w.close()


def test_window_censor_review_flow(window, tmp_path, data_dir, ffmpeg_paths):
    from app.config import AppConfig
    from ui.censor.censor_panel import COL_STATUS, COL_WORD
    from ui.main_window.main_window import MainWindow

    panel = window.censor_panel
    assert not panel.detect_button.isEnabled()  # no transcript yet
    window.transcript.set_transcript(_clip_transcript())
    assert panel.detect_button.isEnabled()

    panel.detect_button.click()
    state = window.censor.state
    assert [s.text for s in state.suggestions] == ["блять", "пиздец"]
    assert window.timeline.timeline.mutes == ()  # detection never mutes
    assert panel.table.rowCount() == 2
    assert window.timeline_panel.view._censor is state

    # Review: accept the first with the keyboard flow, reject the second.
    panel.table.selectRow(0)
    assert window.censor.current == state.suggestions[0].id
    panel.accept_button.click()
    assert window.censor.state.suggestions[0].status == ACCEPTED
    assert window.censor.current == state.suggestions[1].id  # moved to the next one
    panel.reject_button.click()
    assert panel.table.item(1, COL_STATUS).text() == "Rejected"
    assert window.timeline.timeline.mutes == ()  # still nothing until Apply
    assert panel.apply_button.isEnabled()

    # Adjust the end of the first suggestion.
    panel.table.selectRow(0)
    panel.end_spin.setValue(0.9)
    assert window.censor.state.suggestions[0].end_us == 900_000

    panel.mode_combo.setCurrentIndex(panel.mode_combo.findData(BEEP))
    panel.apply_button.click()
    mutes = window.timeline.timeline.mutes
    assert [(m.start_us, m.end_us, m.mode) for m in mutes] == [(150_000, 900_000, BEEP)]
    assert not panel.apply_button.isEnabled() and panel.remove_button.isEnabled()

    # Apply is one undo step for both the timeline and the review.
    window.act_undo.trigger()
    assert window.timeline.timeline.mutes == () and window.censor.state.applied_mute_ids == ()
    window.act_redo.trigger()
    assert len(window.timeline.timeline.mutes) == 1

    # Correcting the recognised word re-runs detection for it.
    panel.table.item(1, COL_WORD).setText("пипец")
    assert [s.text for s in window.censor.state.suggestions] == ["блять"]
    assert window.transcript.transcript.segments[0].words[3].text == "пипец"

    # Exclusions persist in the user's dictionary.
    panel.table.selectRow(0)
    panel.exclude_button.click()
    assert window.censor.state.suggestions == ()
    assert Dictionary.load(window.censor_dictionary_path).exclusions == ["блять"]
    # Its mute stays until the user applies again or removes it.
    assert panel.remove_button.isEnabled()
    panel.remove_button.click()
    assert window.timeline.timeline.mutes == ()

    window.act_undo.trigger()
    window.project_path = tmp_path / "c.tssproj"
    assert window.save()
    other = MainWindow(config=AppConfig.load(data_dir), ffmpeg=ffmpeg_paths)
    other.open_project(tmp_path / "c.tssproj")
    assert _wait(lambda: other.censor.state is not None)
    assert other.censor.state == window.censor.state
    other._dirty = False
    other.close()


def test_preview_range_stops_after_end(window):
    window.preview_range(300_000, 600_000)
    assert window._preview_stop_us == 1_100_000
    window._check_preview_stop(1_200_000)
    assert window._preview_stop_us is None and not window.preview.is_playing


def test_dictionary_dialog(qapp):
    from ui.censor.dictionary_dialog import DictionaryDialog

    dialog = DictionaryDialog(Dictionary(["кринж"], ["бляха"]))
    dialog.words_edit.setPlainText("кринж\n  рофл* \nКРИНЖ\n\n")
    d = dialog.result_dictionary()
    assert d.custom_words == ["кринж", "рофл*"] and d.exclusions == ["бляха"]
