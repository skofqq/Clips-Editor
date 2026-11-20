"""Step pages, part 3: remembered folders, YouTube preview on Data, render queue."""

from __future__ import annotations

import threading
from pathlib import Path

from core.timeline.model import Timeline

S = 1_000_000


def _wait(predicate, timeout=10.0):
    from tests.test_ui import _wait as wait

    return wait(predicate, timeout)


def _window(data_dir, ffmpeg_paths):
    from app.config import AppConfig
    from ui.main_window.main_window import MainWindow

    return MainWindow(config=AppConfig.load(data_dir), ffmpeg=ffmpeg_paths)


def test_folders_are_remembered(qapp, data_dir, ffmpeg_paths, sample_clips, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    from app.config import AppConfig

    w = _window(data_dir, ffmpeg_paths)
    projects = tmp_path / "projects"
    projects.mkdir()
    seen = {}

    def save_name(parent, caption, start, filters):
        seen["save_start"] = start
        return str(projects / "a.tssproj"), ""

    monkeypatch.setattr(QFileDialog, "getSaveFileName", save_name)
    w.import_files([str(sample_clips["mp4"])])
    assert _wait(lambda: w.timeline.timeline is not None)
    assert w.save_as()
    assert AppConfig.load(data_dir).project_dir == str(projects)  # written at once, not on exit

    def open_name(parent, caption, start, filters):
        seen["open_start"] = start
        return "", ""

    monkeypatch.setattr(QFileDialog, "getOpenFileName", open_name)
    w.choose_project()
    assert seen["open_start"] == str(projects)

    # The render folder: changed in the form, kept for the next export.
    renders = tmp_path / "renders"
    renders.mkdir()
    w.act_export.trigger()
    w.export_widget.folder_edit.setText(str(renders))
    w.export_widget.folder_edit.editingFinished.emit()
    assert AppConfig.load(data_dir).export_dir == str(renders)
    w._set_dirty(False)
    w.close()


def test_data_page_youtube_preview(qapp, data_dir, ffmpeg_paths):
    from ui.main_window.pages import DATA

    w = _window(data_dir, ffmpeg_paths)
    w.set_page(DATA)
    w.metadata_panel.title_edit.setText("КОНСТРУКТИВНАЯ ОХОТА | #sasavot on #Twitch")
    w.metadata_panel.description_edit.setPlainText("#shorts #sasavot\n\nhttps://www.twitch.tv/sasavot")
    preview = w.youtube_preview
    assert "КОНСТРУКТИВНАЯ ОХОТА" in preview.title_label.text() and "КОНСТРУКТИВНАЯ" in preview.tile.title
    assert "#shorts" in preview.description_label.text() and "<span" in preview.description_label.text()
    assert "No channel" in preview.channel_label.text()
    w._set_dirty(False)
    w.close()


def _fake_render(source, composition, timeline, settings, output, ffmpeg, ffprobe, encoders=None,
                 progress=None, cancel=None, **kwargs):
    gate = _fake_render.gate
    progress(0.5, "Encoding")
    if gate is not None:
        gate.wait(5)
    if cancel.is_set():
        raise RuntimeError("Cancelled") from None
    Path(output).write_bytes(b"video")
    return object()


def test_render_queue_runs_jobs_in_order(qapp, tmp_path, ffmpeg_paths):
    from ui.export.render_queue import DONE, WAITING, QueuePanel, RenderJob, RenderQueue

    _fake_render.gate = None
    queue = RenderQueue(ffmpeg_paths, runner=_fake_render)
    panel = QueuePanel(queue)
    assert panel.isHidden() is True or not panel.isVisibleTo(panel.parentWidget() or panel)
    t = Timeline.for_source(2 * S)
    jobs = [RenderJob(None, None, t, None, tmp_path / f"{i}.mp4") for i in range(3)]
    for job in jobs:
        queue.add(job)
    assert queue.waiting == 3 and all(j.status == WAITING for j in jobs) and panel.start_button.isEnabled()
    finished = []
    queue.job_finished.connect(finished.append)
    panel.start_button.click()
    assert _wait(lambda: len(finished) == 3)
    assert finished == jobs and all(j.status == DONE and j.output.is_file() for j in jobs)
    assert not queue.running and not queue.busy
    panel.list.setCurrentRow(0)
    assert panel.upload_button.isEnabled()
    panel.clear_button.click()
    assert queue.jobs == []


def test_render_queue_stop_keeps_waiting_jobs(qapp, tmp_path, ffmpeg_paths):
    from ui.export.render_queue import CANCELLED, WAITING, RenderJob, RenderQueue

    _fake_render.gate = threading.Event()
    queue = RenderQueue(ffmpeg_paths, runner=_fake_render)
    first, second = (RenderJob(None, None, None, None, tmp_path / f"{i}.mp4") for i in range(2))
    queue.add(first)
    queue.add(second)
    queue.start()
    assert _wait(lambda: first.progress == 0.5)
    queue.stop()
    _fake_render.gate.set()
    assert _wait(lambda: first.status == CANCELLED)
    assert second.status == WAITING and not queue.busy


def test_window_queues_the_current_edit(qapp, data_dir, ffmpeg_paths, sample_clips, tmp_path, monkeypatch):
    from ui.export import render_queue
    from ui.main_window.pages import RENDER

    _fake_render.gate = None
    w = _window(data_dir, ffmpeg_paths)
    w.render_queue.runner = _fake_render
    w.import_files([str(sample_clips["mp4"])])
    assert _wait(lambda: w.timeline.timeline is not None)
    w.act_export.trigger()
    form = w.export_widget
    assert form.queue_button.isVisibleTo(form)
    form.folder_edit.setText(str(tmp_path))
    form.add_to_queue()
    assert len(w.render_queue.jobs) == 1 and w.queue_panel.isVisibleTo(w.render_page)
    w.queue_panel.start_button.click()
    assert _wait(lambda: w.render_queue.jobs[0].status == render_queue.DONE)
    assert w.page_bar.is_done(RENDER)  # the queued render was of the current edit
    w._set_dirty(False)
    w.close()
