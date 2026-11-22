"""Clip memory: title, description, tags and cover per source clip, and the publication history."""

from __future__ import annotations

from core.metadata.generate import ClipMetadata
from core.projects.clip_memory import INSTAGRAM, YOUTUBE, ClipMemory, Publication
from core.thumbnails.model import ThumbnailDesign
from core.youtube.history import UploadRecord


def test_memory_keeps_each_clip(tmp_path):
    clip = tmp_path / "a.mp4"
    clip.write_bytes(b"x" * 100)
    memory = ClipMemory(tmp_path / "m.json")
    assert not memory.remember(clip, ClipMetadata(), ThumbnailDesign())  # nothing to keep
    assert memory.remember(clip, ClipMetadata(title="ОХОТА | #sasavot", tags=("twitch",)), ThumbnailDesign(offset=12))
    p = memory.add_publication(Publication(YOUTUBE, "https://youtube.com/shorts/v1", "ОХОТА | #sasavot",
                                           "2026-11-21T01:00:00", str(clip), "v1", "Skof"))
    memory.add_publication(p)  # the same video is kept once
    memory.set_cover(p, b"\xff\xd8jpeg")

    again = ClipMemory(tmp_path / "m.json")
    entry = again.get(clip)
    assert entry.metadata.title == "ОХОТА | #sasavot" and entry.metadata.tags == ("twitch",)
    assert entry.thumbnail.offset == 12
    [saved] = again.publications(clip)
    assert saved.video_id == "v1" and again.cover_of(saved).endswith(".jpg")
    # The clip moved to another folder: found by name and size.
    moved = tmp_path / "other"
    moved.mkdir()
    clip.rename(moved / "a.mp4")
    assert again.get(moved / "a.mp4").metadata.title == "ОХОТА | #sasavot"


def test_old_uploads_and_projects_are_read_in(tmp_path):
    from core.projects.project import MediaRef, Project

    clip = tmp_path / "b.mp4"
    clip.write_bytes(b"y" * 10)
    memory = ClipMemory(tmp_path / "m.json")
    record = UploadRecord("2026-11-19T20:00:00", "C:/out/b.mp4", "v2", "https://youtube.com/shorts/v2",
                          "Клатч | #sasavot", "UC1", "Skof", "public")
    assert memory.import_upload_log([record, record]) == 1
    project = Project(media=[MediaRef(path=str(clip), size_bytes=10, duration_s=1.0)],
                      metadata=ClipMetadata(title="Клатч | #sasavot"), thumbnail=ThumbnailDesign(text="КЛАТЧ"))
    assert memory.import_project(project) == 1
    # The old upload has no clip, but its title leads to the clip from the project.
    [old] = memory.publications()
    assert old.source == "" and memory.source_of(old) == str(clip)
    assert memory.publications(clip) == [old]
    assert memory.get(clip).thumbnail.text == "КЛАТЧ"


def test_window_brings_back_title_and_cover(qapp, data_dir, ffmpeg_paths, sample_clips):
    from tests.test_steps import _wait, _window
    from ui.main_window.pages import DATA

    w = _window(data_dir, ffmpeg_paths)
    try:
        first, second = str(sample_clips["mp4"]), str(sample_clips["mkv_noaudio"])
        w.import_files([first])
        assert _wait(lambda: w.preview.media is not None and str(w.preview.media.path) == first)
        w.metadata_panel.title_edit.setText("ОХОТА | #sasavot")
        w.thumbnail_editor.design_changed.emit(ThumbnailDesign(text="ОХОТА", offset=20))
        w._remember_clip()
        w._record_publication(Publication(INSTAGRAM, "https://instagram.com/reel/abc", "ОХОТА | #sasavot",
                                          "2026-11-21T02:00:00", first))
        w.set_page(DATA)
        assert "instagram.com/reel/abc" in w.clip_published_label.text()

        w.import_files([second])
        assert _wait(lambda: str(w.preview.media.path) == second)
        assert w.metadata_panel.metadata().title == ""  # the other clip's title is not carried over
        assert w.project.thumbnail == ThumbnailDesign()
        assert w.clip_published_label.isHidden()

        from pathlib import Path

        w.media_panel.select_media(Path(first))
        assert _wait(lambda: str(w.preview.media.path) == first)
        assert w.metadata_panel.metadata().title == "ОХОТА | #sasavot"
        assert w.project.thumbnail.offset == 20 and w.project.thumbnail.text == "ОХОТА"
        assert w.published_panel.list.count() == 1
    finally:
        w._set_dirty(False)
        w.close()


def test_history_shows_and_sorts_by_numbers(qapp, tmp_path):
    from core.youtube.analytics import VideoStats
    from ui.social.published_panel import VIEWS, WATCHED, PublishedPanel

    memory = ClipMemory(tmp_path / "m.json")
    for n, vid in enumerate(("a", "b", "c")):
        memory.add_publication(Publication(YOUTUBE, f"https://youtube.com/shorts/{vid}", f"Short {vid}",
                                           f"2026-11-2{n}T10:00:00", "", vid, "Skof"))
    memory.add_publication(Publication(INSTAGRAM, "https://instagram.com/reel/x", "Reel x", "2026-11-23T10:00:00"))
    stats = [VideoStats("a", "Short a", "", 30, 12500, 900, 40, avg_view_pct=81.0),
             VideoStats("b", "Short b", "", 30, 300, 20, 1, avg_view_pct=95.5),
             VideoStats("zz", "not ours", "", 30, 10 ** 6, 1, 1)]
    assert memory.set_stats(stats) == 2
    assert ClipMemory(tmp_path / "m.json").stats_for(memory.publications()[-1]).views == 12500  # kept on disk

    panel = PublishedPanel(memory)
    titles = lambda: [panel.list.item(i).text().split("\n")[0] for i in range(panel.list.count())]  # noqa: E731
    assert titles() == ["Reel x", "Short c", "Short b", "Short a"]
    assert "👁 12K" in panel.list.item(3).text() and "81% watched" in panel.list.item(3).text()
    assert "👁" not in panel.list.item(1).text()  # no numbers read for this one yet
    panel.sort_combo.setCurrentIndex(panel.sort_combo.findData(VIEWS))
    assert titles()[:2] == ["Short a", "Short b"]
    panel.sort_combo.setCurrentIndex(panel.sort_combo.findData(WATCHED))
    assert titles()[:2] == ["Short b", "Short a"]
    assert "4 published" in panel.summary_label.text() and "13K views" in panel.summary_label.text()
