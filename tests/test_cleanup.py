"""Replacement rules, capitals, subtitle punctuation."""

from __future__ import annotations

from core.subtitles.model import SubtitleStyle, build_captions
from core.timeline.model import Timeline
from core.transcription.cleanup import clean_transcript, parse_rules, strip_punctuation
from core.transcription.model import SEGMENT_TIMING, Segment, Transcript, Word

S = 1_000_000


def _t(*texts):
    words = tuple(Word(i * S, i * S + 800_000, t) for i, t in enumerate(texts))
    return Transcript("ru", 1.0, "small", (Segment(1, 0, words[-1].end_us, " ".join(texts), words),), 20 * S)


def test_parse_rules():
    rules = parse_rules("скоф = Skof\n# comment\nкс  го -> CS:GO\nгг → GG\nbroken line\n = x\n")
    assert [(r.find, r.replace) for r in rules] == [(("кс", "го"), "CS:GO"), (("скоф",), "Skof"), (("гг",), "GG")]


def test_rules_keep_punctuation_and_timing():
    t = _t("привет", "Скоф,", "давай", "в", "кс", "го!", "гг")
    rules = parse_rules("скоф = Skof\nкс го = CS:GO\nгг = good game")
    out, n = clean_transcript(t, rules)
    words = out.segments[0].words
    assert n == 3
    assert [w.text for w in words] == ["привет", "Skof,", "давай", "в", "CS:GO!", "good", "game"]
    cs = words[4]
    assert (cs.start_us, cs.end_us) == (4 * S, 5 * S + 800_000)  # spans both words
    assert words[5].start_us == 6 * S and words[6].end_us == 6 * S + 800_000 and words[5].end_us == words[6].start_us
    assert out.segments[0].text == "привет Skof, давай в CS:GO! good game"
    assert clean_transcript(t, [])[0] == t


def test_rules_ignore_yo_and_case():
    out, n = clean_transcript(_t("ЁЖИК"), parse_rules("ежик = Ёжик"))
    assert n == 1 and out.segments[0].words[0].text == "Ёжик"


def test_capitalise_sentences():
    t = _t("привет", "ребята.", "как", "дела?", "«ну»", "ok")
    out, _ = clean_transcript(t, capitalise=True)
    assert [w.text for w in out.segments[0].words] == ["Привет", "ребята.", "Как", "дела?", "«Ну»", "ok"]
    rough = Transcript("ru", 1, "s", (Segment(1, 0, S, "x", (Word(0, S, "x", 0.0, SEGMENT_TIMING),)),))
    assert clean_transcript(rough, capitalise=True)[0].segments[0].words[0].timing == SEGMENT_TIMING


def test_subtitles_hide_punctuation_but_keep_sentences():
    assert strip_punctuation("привет,") == "привет" and strip_punctuation("что?!") == "что?!"
    assert strip_punctuation("—") == "—" and strip_punctuation("«ну»") == "ну"
    t = _t("раз,", "два.", "три")
    style = SubtitleStyle(enabled=True, uppercase=False, hide_punctuation=True, max_words=5, gap_split_ms=5000)
    caps = build_captions(t, Timeline.for_source(20 * S), style)
    assert [c.text for c in caps] == ["раз два", "три"]


def test_window_rules_flow(qapp, data_dir, ffmpeg_paths):
    from app.config import AppConfig
    from ui.main_window.main_window import MainWindow
    from ui.transcript.rules_dialog import RulesDialog

    w = MainWindow(config=AppConfig.load(data_dir), ffmpeg=ffmpeg_paths)
    w.transcript.set_transcript(_t("скоф", "привет."))
    assert w.apply_replacement_rules("скоф = Skof", True, True) == 1
    assert [x.text for x in w.transcript.transcript.words()] == ["Skof", "привет."]
    w.act_undo.trigger()
    assert w.transcript.transcript.words()[0].text == "скоф"
    # Fresh recognition results are cleaned automatically.
    w._transcribe_path = None
    assert w._cleaned(_t("скоф"))[0].words()[0].text == "Skof"
    w.config.save()
    assert AppConfig.load(data_dir).transcribe_rules == "скоф = Skof"
    dialog = RulesDialog("a = b\nc = d", True, False)
    assert dialog.count_label.text() == "2 rules" and not dialog.apply_check.isEnabled()
    dialog.close()
    w._dirty = False
    w.close()
