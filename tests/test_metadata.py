"""Phase 8: metadata library, templates, generation, chips, panel and dialogs."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from core.metadata.generate import (
    ClipMetadata,
    GeneratedMetadata,
    MetadataSelection,
    choose_template,
    fit_tags,
    generate,
    hashtag,
    parse_custom,
    format_custom,
)
from core.metadata.library import (
    GAME,
    PARTICIPANT,
    STREAM_TYPE,
    STREAMER,
    TEMPLATE,
    Library,
    MIGRATIONS,
    LibraryError,
    Template,
    entity_from_dict,
    normalize,
)
from core.metadata.template import render, render_title, unknown_variables
from core.projects.project import Project, load_project, save_project
from core.youtube.metadata import tags_length


@pytest.fixture
def lib():
    library = Library(None)
    yield library
    library.close()


def _people(lib):
    streamer = lib.create(STREAMER, "sasavot", username="sasavot", twitch_url="https://www.twitch.tv/sasavot",
                          default_tags=("sasavot", "sasavot clips"))
    helin = lib.create(PARTICIPANT, "helin139", twitch_url="https://www.twitch.tv/helin139", default_tags=("helin",))
    ice = lib.create(PARTICIPANT, "iceicell")
    return streamer, helin, ice


# ------------------------------------------------------------------ library
def test_library_migrates_seeds_and_persists(tmp_path):
    path = tmp_path / "lib.sqlite"
    lib = Library(path)
    assert lib.version == len(MIGRATIONS)
    assert {t.name for t in lib.all(TEMPLATE)} >= {"Streamer — clip #shorts"}
    assert lib.default_template().id == "tpl-sasavot"
    assert [s.name for s in lib.all(STREAM_TYPE)][:2] == ["Challenge", "Gaming"]  # sorted by name
    assert lib.find(GAME, "gta 6").name == "GTA VI"  # alias, any case
    s = lib.create(STREAMER, "Sasavot", username="@sasavot")
    assert s.username == "@sasavot" or s.username == "sasavot"
    lib.close()

    again = Library(path)  # reopening does not reseed or migrate twice
    assert again.version == len(MIGRATIONS)
    assert again.find(STREAMER, "SASAVOT").id == s.id
    assert len(again.all(TEMPLATE)) == 4
    again.close()
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)


def test_library_duplicates_defaults_and_delete(lib):
    a = lib.create(PARTICIPANT, "helin139")
    assert lib.create(PARTICIPANT, "Helin139") == a  # same normalised name: reused
    other = entity_from_dict(PARTICIPANT, {"name": " HELIN139 "})
    with pytest.raises(LibraryError):
        lib.save(other)
    with pytest.raises(LibraryError):
        lib.save(replace(a, name="  "))
    renamed = lib.save(replace(a, name="Helin", default_tags=("x", "X", "#y")))
    assert lib.get(PARTICIPANT, a.id).name == "Helin" and renamed.default_tags == ("x", "y")

    lib.set_default_template("tpl-plays")
    defaults = [t.id for t in lib.all(TEMPLATE) if t.is_default]
    assert defaults == ["tpl-plays"]
    lib.delete(PARTICIPANT, a.id)
    assert lib.get(PARTICIPANT, a.id) is None
    assert normalize("  @Sasa   Vot ") == "sasa vot"


# ----------------------------------------------------------------- template
def test_render_optional_sections_and_labels():
    tpl = "🔥 {clip_title}\n\nGame: {game}\n[[Participants:\n{participants}\n\n]]Full: {stream_url}\n\n\n#tags {{x}}"
    out = render(tpl, {"clip_title": "Clip", "game": "", "participants": "", "stream_url": "u"})
    assert out == "🔥 Clip\n\nFull: u\n\n#tags {x}"
    out = render(tpl, {"clip_title": "Clip", "game": "CS2", "participants": "a, b", "stream_url": ""})
    assert "Game: CS2" in out and "Participants:\na, b" in out and "Full:" not in out
    assert render("Hi {missing} there", {}) == ""  # only empty variables on the line
    assert render("Plain line\n{x}", {}) == "Plain line"
    assert render("{a} {{literal}}", {"a": "{b}"}) == "{b} {literal}"  # values are not re-expanded
    assert unknown_variables("{streamer} {promo} {{nope}}", {"streamer"}) == ["promo"]


def test_render_title_cleans_edges():
    assert render_title("{streamer} — {clip_title} #shorts", {"streamer": "sasavot", "clip_title": ""}) == \
        "sasavot — #shorts"
    assert render_title("{clip_title} | {streamer}", {"clip_title": "", "streamer": "sasavot"}) == "sasavot"
    assert render_title("{streamer} plays {game}[[ with {participants}]]",
                        {"streamer": "s", "game": "CS2", "participants": ""}) == "s plays CS2"
    assert render_title("{a}\n{b}", {"a": "one  two", "b": "x\ny"}) == "one two x y"


# ----------------------------------------------------------------- generate
def test_generate_full_metadata(lib):
    streamer, helin, ice = _people(lib)
    game = lib.find(GAME, "GTA VI")
    sel = MetadataSelection(
        streamer_id=streamer.id, participant_ids=(helin.id, ice.id), game_id=game.id,
        stream_type_ids=("st-2",), clip_title="Угнал танк", date="01.02.2026",
        custom=(("promo", "https://t.me/x"),), project_tags=("tank", "SASAVOT"), extra_hashtags=("GTA VI",),
        template_id="tpl-shorts",
    )
    result = generate(lib, sel)
    assert result.title == "sasavot — Угнал танк #shorts" and result.template_id == "tpl-shorts"
    d = result.description
    assert d.startswith("🔥 Угнал танк\n\nStreamer: sasavot")
    assert "helin139 — https://www.twitch.tv/helin139\niceicell" in d
    assert "Full stream: https://www.twitch.tv/sasavot" in d and d.endswith("#shorts #twitch #GTAVI")
    assert result.tags[:3] == ("sasavot", "sasavot clips", "GTA VI")
    assert len({t.casefold() for t in result.tags}) == len(result.tags) and "tank" in result.tags
    assert result.hashtags == ("#shorts", "#twitch", "#GTAVI") and result.category_id == "20"
    assert not result.warnings

    # Custom variables and the empty-title warning.
    lib.save(Template("t2", "Promo", "{clip_title}", "Promo: {promo}\n{unknown}", is_default=False))
    out = generate(lib, replace(sel, template_id="t2", clip_title=""))
    assert out.description == "Promo: https://t.me/x"
    assert any("{unknown}" in w for w in out.warnings) and any("clip_title" in w for w in out.warnings)


def test_default_sasavot_format(lib):
    streamer, helin, ice = _people(lib)
    nick = lib.create(PARTICIPANT, "nick", twitch_url="https://www.twitch.tv/nick")
    sel = MetadataSelection(streamer_id=streamer.id, participant_ids=(helin.id, ice.id, nick.id),
                            clip_title="КОНСТРУКТИВНАЯ ОХОТА", extra_hashtags=("GTA VI",))
    result = generate(lib, sel)
    assert result.template_id == "tpl-sasavot"
    assert result.title == "КОНСТРУКТИВНАЯ ОХОТА | #sasavot on #Twitch"
    first, links = result.description.split("\n\n")
    # Template hashtags first, then every tag as a hashtag (no repeats).
    assert first.startswith("#shorts #twitch #GTAVI #sasavot #sasavotclips")
    assert "#helin" in first and "#twitchclips" in first and first.count("#sasavot ") == 1
    assert links == "https://www.twitch.tv/sasavot\nhttps://www.twitch.tv/helin139\nhttps://www.twitch.tv/nick"
    alone = generate(lib, replace(sel, participant_ids=()))
    assert alone.description.endswith("\n\nhttps://www.twitch.tv/sasavot")
    by_name = lib.create(PARTICIPANT, "Гость", username="@Guest_1")  # no link, Twitch-style username
    assert generate(lib, replace(sel, participant_ids=(by_name.id,))).description.endswith(
        "https://www.twitch.tv/sasavot\nhttps://www.twitch.tv/guest_1")
    nobody = generate(lib, MetadataSelection(clip_title="x"))
    assert "\n" not in nobody.description and nobody.description.startswith("#shorts")
    assert nobody.title == "x"  # no streamer: no "| on #Twitch" tail
    # Another streamer gets their own hashtag, not #sasavot.
    irbitka = lib.create(STREAMER, "Ирбитка", username="irbitka", twitch_url="https://www.twitch.tv/irbitka")
    other = generate(lib, replace(sel, streamer_id=irbitka.id, participant_ids=()))
    assert other.title == "КОНСТРУКТИВНАЯ ОХОТА | #irbitka on #Twitch"
    assert "sasavot" not in other.description.casefold() and "sasavot" not in " ".join(other.tags)
    assert "#Ирбитка" in other.description
    from core.metadata.generate import MAX_HASHTAGS, all_hashtags
    assert len(all_hashtags((), tuple(f"t{i}" for i in range(100)))) == MAX_HASHTAGS


def test_streamer_can_be_a_participant(lib):
    streamer, helin, _ = _people(lib)
    other = lib.create(STREAMER, "formixyouknow", username="formixyouknow",
                       twitch_url="https://twitch.tv/formixyouknow", default_tags=("формикс",))
    names = [p.name for p in lib.all_people(PARTICIPANT)]
    assert "formixyouknow" in names and "helin139" in names
    sel = MetadataSelection(streamer_id=streamer.id, participant_ids=(other.id, helin.id), clip_title="X")
    result = generate(lib, sel)
    assert result.description.endswith(
        "https://www.twitch.tv/sasavot\nhttps://twitch.tv/formixyouknow\nhttps://www.twitch.tv/helin139")
    assert "формикс" in result.tags
    # And a participant can be the streamer of another clip.
    assert generate(lib, MetadataSelection(streamer_id=helin.id, clip_title="Y")).description.endswith(
        "https://www.twitch.tv/helin139")
    assert lib.person_kind(other.id) == STREAMER and lib.person_kind(helin.id) == PARTICIPANT


def test_old_library_gets_the_new_default(tmp_path):
    path = tmp_path / "old.sqlite"
    with sqlite3.connect(path) as db:
        from core.metadata.library import _migration_1, _migration_2
        _migration_1(db)
        _migration_2(db)
        db.execute("PRAGMA user_version = 2")
    lib = Library(path)
    assert lib.version == len(MIGRATIONS)
    assert "{all_hashtags}" in lib.get(TEMPLATE, "tpl-sasavot").description and lib.default_template().id == "tpl-sasavot"
    assert [t.id for t in lib.all(TEMPLATE) if t.is_default] == ["tpl-sasavot"]
    lib.close()


def test_sasavot_template_follows_the_streamer_after_upgrade(tmp_path):
    from core.metadata.library import _migration_1, _migration_2, _migration_3, _migration_4

    path = tmp_path / "v4.sqlite"
    with sqlite3.connect(path) as db:
        for migration in (_migration_1, _migration_2, _migration_3, _migration_4):
            migration(db)
        row = json.loads(db.execute("SELECT data FROM templates WHERE id = 'tpl-sasavot'").fetchone()[0])
        row.update(title="{clip_title} | #sasavot on #Twitch", name="Clip | #sasavot on #Twitch",
                   default_tags=["sasavot", "twitch", "shorts", "twitch clips"], hashtags=["shorts", "sasavot", "twitch"])
        db.execute("UPDATE templates SET data = ? WHERE id = 'tpl-sasavot'", (json.dumps(row),))
        db.execute("PRAGMA user_version = 4")
    lib = Library(path)
    t = lib.get(TEMPLATE, "tpl-sasavot")
    assert t.title == "{clip_title}[[ | {streamer_hashtag} on #Twitch]]" and t.name == "Clip | #streamer on #Twitch"
    assert "sasavot" not in t.default_tags and t.hashtags == ("shorts", "twitch") and t.is_default
    assert lib.find(TEMPLATE, "Clip | #streamer on #Twitch").id == "tpl-sasavot"
    lib.close()

    # A title skof changed by hand is left alone.
    path2 = tmp_path / "edited.sqlite"
    with sqlite3.connect(path2) as db:
        for migration in (_migration_1, _migration_2, _migration_3, _migration_4):
            migration(db)
        row = json.loads(db.execute("SELECT data FROM templates WHERE id = 'tpl-sasavot'").fetchone()[0])
        row["title"] = "{clip_title} | мой формат"
        db.execute("UPDATE templates SET data = ? WHERE id = 'tpl-sasavot'", (json.dumps(row),))
        db.execute("PRAGMA user_version = 4")
    lib = Library(path2)
    assert lib.get(TEMPLATE, "tpl-sasavot").title == "{clip_title} | мой формат"
    lib.close()


def test_template_choice_order(lib):
    streamer, _, _ = _people(lib)
    game = lib.find(GAME, "CS2")
    sel = MetadataSelection(streamer_id=streamer.id, game_id=game.id)
    assert choose_template(lib, sel).id == "tpl-sasavot"  # library default
    lib.save(replace(game, template_id="tpl-plays"))
    assert choose_template(lib, sel).id == "tpl-plays"
    lib.save(replace(lib.get(STREAMER, streamer.id), template_id="tpl-clip-first"))
    assert choose_template(lib, sel).id == "tpl-clip-first"
    assert choose_template(lib, replace(sel, template_id="tpl-plays")).id == "tpl-plays"
    assert choose_template(lib, replace(sel, template_id="deleted")).id == "tpl-clip-first"


def test_helpers():
    assert hashtag("GTA VI") == "#GTAVI" and hashtag("#катка!") == "#катка" and hashtag("  ") == ""
    custom = parse_custom("promo = https://t.me/x\nbad line\n{tg}= @me\n1x = no\nmulti = a\\nb")
    assert custom == {"promo": "https://t.me/x", "tg": "@me", "multi": "a\nb"}
    assert parse_custom(format_custom(custom)) == custom
    many = tuple(f"tag number {i}" for i in range(60))
    fitted = fit_tags(many)
    assert tags_length(fitted) <= 500 and fitted == many[: len(fitted)]


def test_clip_metadata_edits_and_project_roundtrip(tmp_path):
    gen = GeneratedMetadata("T", "D", ("a", "b"), ("#x",), "20", "tpl-shorts")
    clip = ClipMetadata(MetadataSelection(clip_title="c"), None).with_generated(gen)
    assert (clip.title, clip.description, clip.tags) == ("T", "D", ("a", "b")) and clip.edited_fields() == []
    edited = replace(clip, title="My title", tags=("a",))
    assert edited.edited_fields() == ["title", "tags"]
    gen2 = replace(gen, title="T2", description="D2", tags=("c",))
    kept = edited.with_generated(gen2, keep=("title",))
    assert (kept.title, kept.description, kept.tags) == ("My title", "D2", ("c",))
    assert ClipMetadata.from_dict(kept.to_dict()) == kept

    project = Project(name="p", metadata=kept)
    path = save_project(project, tmp_path / "p.tssproj")
    assert load_project(path).metadata == kept
    assert "metadata" not in Project().to_dict()


# -------------------------------------------------------------------- chips
def test_chip_input(qapp):
    from ui.chips.chip_input import ChipInput

    created, edited, changes = [], [], []
    chips = ChipInput("Game…", single=True)
    chips.set_suggestions([("g1", "GTA VI", ("GTA 6",)), ("g2", "CS2")])
    chips.create_requested.connect(created.append)
    chips.edit_requested.connect(edited.append)
    chips.changed.connect(lambda: changes.append(1))
    chips.add_text("gta 6")
    assert chips.items() == [("g1", "GTA VI")] and chips.edit.text() == ""
    chips.add_text("CS2")
    assert chips.ids() == ("g2",)  # single: replaced
    chips.add_text("Valorant")
    assert created == ["Valorant"] and chips.ids() == ("g2",)
    chips.chip("g2").activated.emit("g2")
    assert edited == ["g2"]
    chips.chip("g2").close_button.click()
    assert chips.items() == [] and len(changes) == 3

    tags = ChipInput(free_text=True)
    tags.add_text("twitch, #gaming; Twitch ,  , gta  vi")
    assert tags.labels() == ("twitch", "gaming", "gta vi")
    tags.set_items([("a", "a")])
    assert tags.labels() == ("a",) and tags.flow.count() == 2  # chip + text field


def test_chip_input_picking_a_suggestion_clears_the_field(qapp):
    """Choosing from the drop-down (Enter or click) leaves no text after the chip."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from ui.chips.chip_input import ChipInput

    for free_text in (False, True):
        chips = ChipInput("Game…", single=not free_text, free_text=free_text)
        chips.set_suggestions([("g1", "Just Chatting"), ("g2", "Dota 2")])
        chips.show()
        QTest.qWaitForWindowExposed(chips)
        chips.edit.setFocus()
        QTest.keyClicks(chips.edit, "just")
        popup = chips.completer.popup()
        QTest.keyClick(popup, Qt.Key.Key_Down)
        QTest.keyClick(popup, Qt.Key.Key_Return)
        QTest.qWait(20)
        assert chips.labels() == ("Just Chatting",) and chips.edit.text() == ""

        QTest.keyClicks(chips.edit, "dot")
        rect = popup.visualRect(chips.completer.completionModel().index(0, 0))
        QTest.mouseClick(popup.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
        QTest.qWait(20)
        assert "Dota 2" in chips.labels() and chips.edit.text() == ""
        chips.close()


# -------------------------------------------------------------------- panel
def test_metadata_panel_follows_a_new_streamer(qapp, lib):
    """Changing the Streamer chip rebuilds title/description/tags; hand edits keep, with the hashtag swapped."""
    from ui.metadata.metadata_panel import MetadataPanel

    _people(lib)
    irbitka = lib.create(STREAMER, "irbitka", username="irbitka", twitch_url="https://www.twitch.tv/irbitka")
    panel = MetadataPanel(lib)
    panel.carry_check.setChecked(False)
    panel.streamer_chips.add_text("sasavot")
    assert panel.title_edit.text() == ""  # nothing generated yet: picking a streamer alone does not fill it
    panel.clip_title_edit.setText("АУДИТОРИЯ/ГОДА")
    assert panel.generate_metadata()
    assert panel.title_edit.text() == "АУДИТОРИЯ/ГОДА | #sasavot on #Twitch"

    panel.streamer_chips.add_text("irbitka")  # single: replaces sasavot
    meta = panel.metadata()
    assert meta.selection.streamer_id == irbitka.id
    assert meta.title == "АУДИТОРИЯ/ГОДА | #irbitka on #Twitch" and meta.edited_fields() == []
    assert "irbitka" in meta.tags and "sasavot" not in meta.tags
    assert "https://www.twitch.tv/irbitka" in meta.description and "sasavot" not in meta.description

    panel.title_edit.setText("Моя версия | #irbitka on #Twitch")  # a hand edit
    panel.streamer_chips.add_text("sasavot")
    assert panel.title_edit.text() == "Моя версия | #sasavot on #Twitch"
    assert "Kept your edits: title" in panel.status_label.text()


def test_metadata_panel_generates_and_keeps_edits(qapp, lib):
    from ui.metadata.metadata_panel import MetadataPanel

    lib.set_default_template("tpl-shorts")  # the long description format these checks read
    panel = MetadataPanel(lib)
    emitted = []
    panel.metadata_changed.connect(emitted.append)
    panel.streamer_chips.add_text("sasavot")  # not in the library: created on the fly
    streamer = lib.find(STREAMER, "sasavot")
    assert streamer is not None and panel.streamer_chips.ids() == (streamer.id,)
    assert "Double-click" in panel.status_label.text()
    panel.participant_chips.add_text("helin139")
    panel.game_chips.add_text("gta 5")
    panel.type_chips.add_text("IRL")
    panel.clip_title_edit.setText("Угнал танк")
    assert emitted and emitted[-1].selection.clip_title == "Угнал танк"

    assert panel.generate_metadata()
    meta = panel.metadata()
    assert meta.title == "sasavot — Угнал танк #shorts"
    assert "GTA V" in meta.tags and "irl" in meta.tags and "Game: GTA V" in meta.description
    assert "#shorts" in panel.hashtags_label.text() and meta.edited_fields() == []

    panel.title_edit.setText("Мой заголовок")
    assert "edited by you" in panel.title_count.text()
    panel.clip_title_edit.setText("Другой клип")
    asked = []
    panel.ask_keep_edits = lambda fields: asked.append(fields) or tuple(fields)
    assert panel.generate_metadata()
    assert asked == [["title"]] and panel.title_edit.text() == "Мой заголовок"
    assert "Другой клип" in panel.description_edit.toPlainText() and "Kept your edits" in panel.status_label.text()

    panel.ask_keep_edits = lambda fields: None  # cancel changes nothing
    panel.clip_title_edit.setText("Третий")
    assert not panel.generate_metadata() and "Другой клип" in panel.description_edit.toPlainText()
    panel.ask_keep_edits = lambda fields: ()  # replace all
    assert panel.generate_metadata() and panel.title_edit.text() == "sasavot — Третий #shorts"

    # Loading a saved state restores chips and fields without emitting.
    saved = panel.metadata()
    other = MetadataPanel(lib)
    count = len(emitted)
    other.set_metadata(saved)
    assert other.metadata() == saved and len(emitted) == count
    assert other.streamer_chips.labels() == ("sasavot",) and other.type_chips.labels() == ("IRL",)

    # A deleted profile disappears from the chips after a library refresh.
    lib.delete(PARTICIPANT, lib.find(PARTICIPANT, "helin139").id)
    other.refresh_library()
    assert other.participant_chips.items() == []


# ------------------------------------------------------------------- dialog
def test_library_dialog_edits_profiles_and_templates(qapp, lib, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from ui.metadata.library_dialog import LibraryDialog

    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
    dialog = LibraryDialog(lib)
    page = dialog.pages[STREAMER]
    dialog.create(STREAMER, "sasavot")
    page.username_edit.setText("sasavot")
    page.twitch_edit.setText("https://www.twitch.tv/sasavot")
    page.links_edit.setPlainText("Telegram | https://t.me/sasavot\nhttps://vk.com/x")
    page.tags_edit.setText("sasavot, clips, Clips")
    assert page.save_current()
    s = lib.find(STREAMER, "sasavot")
    assert s.social_links == (("Telegram", "https://t.me/sasavot"), ("", "https://vk.com/x"))
    assert s.default_tags == ("sasavot", "clips") and page.list.count() == 1
    assert s.playlist == "" and not s.playlist_off and "sasavot" in page.playlist_edit.placeholderText()
    page.playlist_edit.setText("Клипы Сасавот")
    page.playlist_off_check.setChecked(True)
    assert not page.playlist_edit.isEnabled()
    assert page.save_current()
    s = lib.find(STREAMER, "sasavot")
    assert (s.playlist, s.playlist_off) == ("Клипы Сасавот", True)
    assert not dialog.pages[GAME].playlist_edit.isVisibleTo(dialog.pages[GAME])

    page.new_entry("SASAVOT")  # duplicate: refused with a message, nothing saved
    assert not page.save_current() and "already exists" in page.status.text()
    assert len(lib.all(STREAMER)) == 1

    tpl_page = dialog.show_kind(TEMPLATE)
    assert tpl_page.select("tpl-plays")
    tpl_page.title_edit.setText("{streamer} в {game} — {clip_title}")
    tpl_page.title_edit.textEdited.emit(tpl_page.title_edit.text())
    assert "sasavot в GTA VI — Угнал танк у военных" in tpl_page.preview_label.text()
    tpl_page.description_edit.setPlainText("{nope}")
    assert "Unknown variable {nope}" in tpl_page.preview_label.text()
    dialog.accept()  # autosaves the template being edited
    assert lib.get(TEMPLATE, "tpl-plays").title == "{streamer} в {game} — {clip_title}"
    assert len(lib.all(TEMPLATE)) == 4  # the preview never wrote to the real library

    dialog = LibraryDialog(lib)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    dialog.edit(STREAMER, s.id)
    dialog.pages[STREAMER].delete_current()
    assert lib.all(STREAMER) == []
    dialog.close()


# ------------------------------------------------------------------- window
def _wait(predicate, timeout=10.0):
    import time

    from PySide6.QtWidgets import QApplication

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_window_stores_metadata_and_prefills_upload(qapp, data_dir, ffmpeg_paths, sample_clips, tmp_path,
                                                    monkeypatch):
    from app.config import AppConfig
    from ui.main_window.main_window import MainWindow
    from ui.youtube import upload_dialog as upload_mod

    w = MainWindow(config=AppConfig.load(data_dir), ffmpeg=ffmpeg_paths)
    assert (data_dir / "metadata_library.sqlite").exists()
    assert w.metadata_panel.window() is w  # on the Data page
    panel = w.metadata_panel
    panel.streamer_chips.add_text("sasavot")
    panel.clip_title_edit.setText("Танк")
    assert w._dirty
    assert panel.generate_metadata()
    assert w.project.metadata.title == "Танк | #sasavot on #Twitch"

    w.project_path = tmp_path / "m.tssproj"
    assert w.save()
    w.new_project()
    assert panel.metadata().title == "" and panel.streamer_chips.items() == []
    w.open_project(tmp_path / "m.tssproj")
    assert panel.title_edit.text() == "Танк | #sasavot on #Twitch" and not w._dirty

    rendered = tmp_path / "out.mp4"
    rendered.write_bytes(Path(sample_clips["mp4"]).read_bytes())
    w.upload_to_youtube(rendered)  # the Publish page with the form filled from Data
    from ui.main_window.pages import PUBLISH

    form = w.upload_widget
    assert w.page == PUBLISH and isinstance(form, upload_mod.UploadDialog) and not form.isWindow()
    assert form.title_edit.text() == "Танк | #sasavot on #Twitch"
    assert form.playlist == "sasavot" and form.playlist_check.isChecked()  # the streamer's playlist
    # The chosen category is kept for the next upload, over the template's one.
    form.category_combo.setCurrentIndex(form.category_combo.findData("24"))
    assert w.config.youtube_defaults["category"] == "24"
    w._show_upload_form(rendered, fresh=True)
    assert w.upload_widget.category_combo.currentData() == "24"
    w._dirty = False
    w.close()


def test_upload_dialog_uses_project_metadata(qapp, data_dir, tmp_path, ffmpeg_paths, sample_clips):
    from app.config import AppConfig
    from core.youtube.history import UploadHistory
    from core.youtube.service import YouTubeService
    from core.youtube.storage import MemoryStore
    from ui.youtube.upload_dialog import UploadDialog

    service = YouTubeService(AppConfig.load(data_dir), MemoryStore())
    rendered = tmp_path / "clip.mp4"
    rendered.write_bytes(Path(sample_clips["mp4"]).read_bytes())
    gen = GeneratedMetadata("Gen", "Desc", ("a",), (), "24", "")
    meta = ClipMetadata(generated=gen, title="Мой заголовок", description="Описание", tags=("twitch", "gta vi"))
    dialog = UploadDialog(service, UploadHistory(tmp_path / "h.json"), rendered, ffmpeg_paths.ffprobe,
                          {"tags": "old", "category": "20"}, metadata=meta)
    m = dialog.metadata()
    # The category chosen last time wins over the template's one.
    assert (m.title, m.description, m.tags, m.category_id) == ("Мой заголовок", "Описание", ("twitch", "gta vi"), "20")
    assert "Data page" in dialog.note_label.text()
    assert dialog.defaults()["tags"] == "old"  # project tags do not leak into the global defaults
    dialog.close()

    plain = UploadDialog(service, UploadHistory(tmp_path / "h.json"), rendered, ffmpeg_paths.ffprobe,
                         {"tags": "old"}, metadata=ClipMetadata())
    assert plain.metadata().title == "clip" and plain.metadata().tags == ("old",)
    plain.close()

    # The streamer's playlist: ticked by default, kept off once unticked, empty without a streamer.
    named = UploadDialog(service, UploadHistory(tmp_path / "h.json"), rendered, ffmpeg_paths.ffprobe,
                         {}, metadata=meta, playlist="sasavot")
    assert named.playlist_check.isChecked() and "sasavot" in named.playlist_check.text()
    assert named.queue_entry()["playlist"] == "sasavot"
    named.playlist_check.setChecked(False)
    assert named.chosen_playlist() == "" and named.defaults()["playlist"] is False
    named.close()
    off = UploadDialog(service, UploadHistory(tmp_path / "h.json"), rendered, ffmpeg_paths.ffprobe,
                       {"playlist": False}, metadata=meta, playlist="sasavot")
    assert not off.playlist_check.isChecked()
    off.close()
    assert not plain.playlist_check.isEnabled() and plain.chosen_playlist() == "" 
