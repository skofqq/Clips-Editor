"""0.9.35: participants recognised by voice, and metadata copied from the streamer's last clip."""

from __future__ import annotations

import numpy as np
import pytest

from core.metadata.carry_over import carry_over, from_last_clip, fresh, guess_streamer, last_clip_of
from core.metadata.generate import ClipMetadata, MetadataSelection, generate
from core.metadata.library import GAME, PARTICIPANT, STREAM_TYPE, STREAMER, TEMPLATE, Library
from core.metadata.voices import MIN_WINDOWS, VoiceBook, speech_seconds
from core.projects.clip_memory import ClipMemory
from core.thumbnails.model import ThumbnailDesign


def _vec(seed: int, noise: float = 0.0, other: int | None = None) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.normal(size=192)
    if noise:
        v = v + np.random.default_rng(other if other is not None else seed + 1000).normal(size=192) * noise
    return v / np.linalg.norm(v)


@pytest.fixture
def lib(tmp_path):
    library = Library(tmp_path / "lib.sqlite")
    yield library
    library.close()


# ------------------------------------------------------------------ voices
def test_named_voice_is_recognised_in_the_next_clip(tmp_path):
    book = VoiceBook(tmp_path / "voices.json")
    mira, lexa = _vec(1), _vec(2)
    clip, matches = book.set_clip("C:/a.mp4", {0: (mira, 10), 1: (lexa, 8)}, {0: 7.5, 1: 4.0})
    assert matches == [] and clip.voices[0].seconds == 7.5
    assert book.assign("C:/a.mp4", 0, "p-mira")
    assert book.assign("C:/a.mp4", 1, "p-lexa")

    # Another clip: the same voices a little different, in the other order, plus a stranger.
    again = VoiceBook(tmp_path / "voices.json")  # read back from the file
    clip, matches = again.set_clip("C:/b.mp4", {0: (_vec(2, 0.4), 6), 1: (_vec(1, 0.4), 9), 2: (_vec(3), 9)})
    assert {(m.speaker, m.person_id) for m in matches} == {(0, "p-lexa"), (1, "p-mira")}
    assert clip.voices[1].auto and clip.voices[1].similarity > 0.5
    assert clip.voices[2].person_id == ""
    # Only the people that still exist are recognised.
    _, matches = again.set_clip("C:/b.mp4", {0: (_vec(2, 0.4), 6)}, allowed={"p-mira"})
    assert matches == []


def test_recognised_voices_do_not_teach_and_unnaming_forgets(tmp_path):
    book = VoiceBook(None)
    book.set_clip("a", {0: (_vec(1), 10)})
    book.assign("a", 0, "p")
    book.set_clip("b", {0: (_vec(1, 0.3), 10)})
    assert book.get("b").voices[0].auto
    before = book.voiceprints()["p"].copy()
    assert np.allclose(book.voiceprints()["p"], before)  # the guess did not change the voiceprint
    book.assign("a", 0, "")
    assert book.voiceprints() == {}
    book.set_clip("c", {0: (_vec(1), 10)})
    assert book.get("c").voices[0].person_id == ""


def test_short_voices_are_not_learned_and_one_person_per_clip(tmp_path):
    book = VoiceBook(None)
    book.set_clip("a", {0: (_vec(1), MIN_WINDOWS - 1), 1: (_vec(2), 10)})
    book.assign("a", 0, "p")
    assert "p" not in book.voiceprints()
    book.assign("a", 1, "p")  # the same person picked for another speaker: the first loses them
    assert book.get("a").people() == {1: "p"}


def test_speech_seconds():
    from core.transcription.model import Segment, Transcript, Word

    words = (Word(0, 1_000_000, "a", 1.0, speaker=0), Word(1_000_000, 1_500_000, "b", 1.0, speaker=1),
             Word(1_500_000, 2_000_000, "c", 1.0))
    t = Transcript("ru", 1.0, "test", segments=(Segment(0, 0, 2_000_000, "a b c", words),))
    assert speech_seconds(t) == {0: 1.0, 1: 0.5}


def test_diarization_gives_voices():
    from core.transcription.diarization import SpeakerLabels, speaker_voices

    emb = np.stack([_vec(1), _vec(1, 0.1), _vec(2), _vec(1, 0.1, 7)])
    voices = speaker_voices(emb, [0, 0, 1, 0])
    assert voices[0][1] == 3 and voices[1][1] == 1
    assert abs(np.linalg.norm(voices[0][0]) - 1) < 1e-5
    labels = SpeakerLabels([0, 1], voices)
    assert labels == [0, 1] and labels.voices is not voices and set(labels.voices) == {0, 1}


# --------------------------------------------------------------- last clip
def _setup(lib):
    sasa = lib.create(STREAMER, "Sasavot", username="sasavot")
    other = lib.create(STREAMER, "Evelone", username="evelone192")
    mira = lib.create(PARTICIPANT, "Mira")
    game = lib.create(GAME, "GTA V")
    stream_type = lib.all(STREAM_TYPE)[0]
    template = lib.all(TEMPLATE)[0]
    return sasa, other, mira, game, stream_type, template


def _previous(lib, sasa, mira, game, stream_type, template) -> ClipMetadata:
    selection = MetadataSelection(streamer_id=sasa.id, participant_ids=(mira.id,), game_id=game.id,
                                  stream_type_ids=(stream_type.id,), template_id=template.id,
                                  clip_title="Клатч", date="20.11.2026", stream_url="https://twitch.tv/videos/1",
                                  project_tags=("охота",), extra_hashtags=("гта",))
    return ClipMetadata(selection).with_generated(generate(lib, selection))


def test_carry_over_copies_choices_not_the_clip_itself(lib):
    sasa, _, mira, game, stream_type, template = _setup(lib)
    previous = _previous(lib, sasa, mira, game, stream_type, template)
    result = carry_over(ClipMetadata(MetadataSelection(streamer_id=sasa.id)), previous, lib)
    s = result.selection
    assert (s.participant_ids, s.game_id, s.stream_type_ids, s.template_id) == \
        ((mira.id,), game.id, (stream_type.id,), template.id)
    assert s.project_tags == ("охота",) and s.extra_hashtags == ("гта",)
    assert s.clip_title == "" and s.date == "" and s.stream_url == ""
    assert result.title == "" and result.edited_fields() == []  # Generate later just fills the title
    assert "GTA V" in result.tags and result.description


def test_carry_over_keeps_the_users_own_description(lib):
    sasa, _, mira, game, stream_type, template = _setup(lib)
    previous = _previous(lib, sasa, mira, game, stream_type, template)
    edited = previous.__class__(previous.selection, previous.generated, previous.title,
                                "#shorts #sasavot\n\nмой телеграм t.me/x", ("sasavot", "мой тег"))
    result = carry_over(ClipMetadata(MetadataSelection(streamer_id=sasa.id)), edited, lib)
    assert result.description.endswith("t.me/x") and result.tags == ("sasavot", "мой тег")
    # A description about that clip (has its clip title) is not copied.
    about = previous.__class__(previous.selection, previous.generated, previous.title, "Клатч на 1 хп", ())
    assert "Клатч" not in carry_over(ClipMetadata(MetadataSelection(streamer_id=sasa.id)), about, lib).description


def test_from_last_clip_picks_the_streamers_newest_clip(lib, tmp_path):
    sasa, other, mira, game, stream_type, template = _setup(lib)
    memory = ClipMemory(tmp_path / "m.json")
    previous = _previous(lib, sasa, mira, game, stream_type, template)
    memory.remember("C:/old.mp4", previous, ThumbnailDesign())
    newer = ClipMetadata(MetadataSelection(streamer_id=sasa.id, game_id=game.id))
    memory.remember("C:/new.mp4", newer, ThumbnailDesign())
    memory.remember("C:/other.mp4", ClipMetadata(MetadataSelection(streamer_id=other.id, game_id=game.id)),
                    ThumbnailDesign())
    memory._clips[next(k for k in memory._clips if "old" in k)] = \
        memory.get("C:/old.mp4").__class__("C:/old.mp4", previous, ThumbnailDesign(), "2026-11-20T10:00:00")
    memory._clips[next(k for k in memory._clips if "new" in k)] = \
        memory.get("C:/new.mp4").__class__("C:/new.mp4", newer, ThumbnailDesign(), "2026-11-21T10:00:00")
    memory._clips[next(k for k in memory._clips if "other" in k)] = \
        memory.get("C:/other.mp4").__class__("C:/other.mp4", memory.get("C:/other.mp4").metadata,
                                             ThumbnailDesign(), "2026-11-22T10:00:00")

    assert last_clip_of(memory, sasa.id).source == "C:/new.mp4"
    assert last_clip_of(memory, sasa.id, exclude="c:/NEW.mp4").source == "C:/old.mp4"
    assert last_clip_of(memory).source == "C:/other.mp4"
    result = from_last_clip(ClipMetadata(), memory, lib, "C:/third.mp4", sasa.id)
    assert result.source == "C:/new.mp4" and result.metadata.selection.game_id == game.id
    assert result.metadata.selection.streamer_id == sasa.id
    # Nothing is copied into a clip that already has choices, or without a streamer.
    assert from_last_clip(ClipMetadata(title="x"), memory, lib, "", sasa.id) is None
    assert from_last_clip(ClipMetadata(), memory, lib) is None
    assert fresh(ClipMetadata(MetadataSelection(streamer_id="x", clip_title="y")))


def test_guess_streamer_from_the_file_name(lib):
    sasa, other, *_ = _setup(lib)
    assert guess_streamer(lib, "sasavot_ОХОТА-20251120.mp4") == sasa.id
    assert guess_streamer(lib, "Clip evelone192 - clutch.mkv") == other.id
    assert guess_streamer(lib, "stream clip.mp4") == ""
    assert guess_streamer(lib, "sasavot and evelone192.mp4") == ""  # two of them: no guess


# --------------------------------------------------------------------- UI
def test_panel_fills_from_last_clip_when_the_streamer_is_picked_and_undo(qapp, lib, tmp_path):
    from ui.metadata.metadata_panel import MetadataPanel

    sasa, _, mira, game, stream_type, template = _setup(lib)
    memory = ClipMemory(tmp_path / "m.json")
    memory.remember("C:/old.mp4", _previous(lib, sasa, mira, game, stream_type, template), ThumbnailDesign())
    panel = MetadataPanel(lib)
    panel.previous_provider = lambda meta, sid: from_last_clip(meta, memory, lib, "C:/now.mp4", sid)
    seen = []
    panel.metadata_changed.connect(seen.append)
    panel.streamer_chips.add_item(sasa.id, sasa.name)
    meta = panel.metadata()
    assert meta.selection.game_id == game.id and panel.participant_chips.ids() == (mira.id,)
    assert not panel.carry_row.isHidden() and "old.mp4" in panel.carry_label.text()
    assert seen[-1] == meta
    panel.undo_carry_over()
    assert panel.metadata().selection == MetadataSelection(streamer_id=sasa.id)
    assert panel.carry_row.isHidden()

    # Switched off: picking the streamer fills nothing.
    panel.set_metadata(ClipMetadata())
    panel.carry_check.setChecked(False)
    panel.streamer_chips.add_item(sasa.id, sasa.name)
    assert panel.metadata().selection.game_id == ""


def test_panel_voices_add_people(qapp, lib):
    from core.metadata.voices import VoiceBook
    from ui.metadata.metadata_panel import MetadataPanel

    sasa, _, mira, *_ = _setup(lib)
    panel = MetadataPanel(lib)
    book = VoiceBook(None)
    clip, _ = book.set_clip("a", {0: (_vec(1), 10), 1: (_vec(2), 10)}, {0: 12.0})
    panel.set_voices(clip)
    assert set(panel.voices_box.combos) == {0, 1}
    assert panel.voices_box.find_button.text() == "Find again"
    got = []
    panel.voices_box.assigned.connect(lambda s, p: got.append((s, p)))
    combo = panel.voices_box.combos[1]
    combo.setCurrentIndex(combo.findData(mira.id))
    combo.activated.emit(combo.currentIndex())
    assert got == [(1, mira.id)]
    # A streamer profile fills the empty streamer field, a participant goes to participants.
    assert panel.add_person(sasa.id) == "streamer"
    assert panel.add_person(mira.id) == "participant"
    assert panel.add_person(mira.id) == ""
    assert panel.metadata().selection.participant_ids == (mira.id,)
    panel.set_voices(None)
    assert panel.voices_box.combos == {} and panel.voices_box.find_button.text() == "Find voices"


def test_window_voices_and_last_clip(qapp, data_dir, ffmpeg_paths, sample_clips, monkeypatch):
    from core.transcription import diarization
    from core.transcription.diarization import SpeakerLabels
    from tests.test_diarization import _conversation
    from tests.test_steps import _wait, _window

    w = _window(data_dir, ffmpeg_paths)
    try:
        lib = w.metadata_library
        sasa = lib.create(STREAMER, "Sasavot", username="sasavot")
        mira = lib.create(PARTICIPANT, "Mira")
        game = lib.create(GAME, "GTA V")
        w.metadata_panel.refresh_library()
        first, second = str(sample_clips["mp4"]), str(sample_clips["mkv_noaudio"])
        w.import_files([first])
        assert _wait(lambda: w.preview.media is not None and str(w.preview.media.path) == first)

        # Find speakers keeps the voices; naming one teaches it and puts Mira on the clip.
        voices = {0: (_vec(1), 10), 1: (_vec(2), 10)}
        monkeypatch.setattr(diarization, "find_speakers_in_file",
                            lambda models, path, ffmpeg, t, count, progress=None, cancel=None:
                            SpeakerLabels([k % 2 for k in range(len(t.words()))], voices))
        transcript, _ = _conversation()
        w.transcript.set_transcript(transcript)
        w.metadata_panel.voices_box.find_button.click()
        assert _wait(lambda: w._transcribe_task is None and w.voices.book.get(first) is not None)
        assert set(w.metadata_panel.voices_box.combos) == {0, 1}
        w.metadata_panel.voices_box.assigned.emit(1, mira.id)
        assert w.metadata_panel.metadata().selection.participant_ids == (mira.id,)
        w.metadata_panel.streamer_chips.add_item(sasa.id, sasa.name)
        w.metadata_panel.game_chips.add_item(game.id, game.name)
        w._remember_clip()

        # A new clip: filled from Sasavot's last clip (the clip edited last), Mira's voice recognised.
        w.import_files([second])
        assert _wait(lambda: str(w.preview.media.path) == second)
        meta = w.metadata_panel.metadata()
        assert meta.selection.streamer_id == sasa.id and meta.selection.game_id == game.id
        assert meta.title == "" and not w.metadata_panel.carry_row.isHidden()
        assert w.project.metadata == meta
        w.metadata_panel.undo_carry_over()
        assert w.metadata_panel.metadata().selection.participant_ids == ()
        voices.clear()
        voices.update({0: (_vec(9), 10), 1: (_vec(2, 0.3), 10)})
        w.transcript.set_transcript(transcript)
        w.find_speakers(None)
        assert _wait(lambda: w._transcribe_task is None and w.voices.book.get(second) is not None)
        assert w.metadata_panel.metadata().selection.participant_ids == (mira.id,)
        assert "Recognised by voice: Mira" in w.metadata_panel.status_label.text()
        assert "recognised by voice" in w.metadata_panel.voices_box.notes[1].text()
    finally:
        w._set_dirty(False)
        w.close()
