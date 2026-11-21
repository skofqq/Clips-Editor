"""The Data page's thumbnail: which frame, grabbing it off the GUI thread,
the preview, saving the picture and the JPEG for the upload."""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QObject, QThreadPool
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QFileDialog, QMessageBox

from core.thumbnails.draw import candidate_frames, candidate_times, jpeg_bytes, make_thumbnail, save_thumbnail
from core.thumbnails.frame import grab_frame
from core.workers import Worker

log = logging.getLogger(__name__)


class ThumbnailKeeper(QObject):
    def __init__(self, window, editor) -> None:
        super().__init__(window)
        self.w = window
        self.editor = editor
        self._frame: tuple[tuple, bytes] | None = None  # (key, PNG data) of the last grabbed frame
        self._task: Worker | None = None  # kept so its signals live until it reports
        self._task_key: tuple | None = None
        self._running: set[Worker] = set()  # every started grab, kept until it reports
        self.image: QImage | None = None  # the finished thumbnail shown now
        editor.design_changed.connect(self._design_changed)
        editor.frame_requested.connect(self.use_playhead)
        editor.frame_chosen.connect(self.use_frame)
        editor.scrub_moved.connect(self._scrubbed)
        self._candidates_key: tuple | None = None  # what the suggested frames were made for
        editor.save_requested.connect(self.save_as)

    # ------------------------------------------------------------ source
    def _composition_at(self, us: int):
        timeline = self.w.timeline.timeline
        layout = None
        if timeline is not None and timeline.segments:
            layout = timeline.segments[timeline.layout_index_at(us)].layout
        return (layout or self.w.main_layout).at(us)

    def _length_us(self) -> int:
        timeline, media = self.w.timeline.timeline, self.w.preview.media
        if timeline is not None:
            return timeline.output_us
        return int(media.duration_s * 1e6) if media is not None else 0

    def _to_source(self, out_us: int) -> int:
        timeline = self.w.timeline.timeline
        return timeline.output_to_source(out_us) if timeline is not None else int(out_us)

    def _to_output(self, us: int) -> int:
        timeline = self.w.timeline.timeline
        return timeline.source_to_output(us) if timeline is not None else int(us)

    def source(self):
        """(video, source_us, composition, src_w, src_h) of the chosen frame, or None."""
        media = self.w.preview.media
        if media is None or not media.has_video or self.w.ffmpeg is None:
            return None
        design = self.w.project.thumbnail
        us = design.source_us
        if us is None:
            us = self._to_source(self._length_us() // 4)
        composition = self._composition_at(us)
        src_w, src_h = self.w.composition.source_size
        return media.path, int(us), composition, src_w, src_h

    def _blurs(self) -> tuple:
        timeline = self.w.timeline.timeline
        return timeline.blurs if timeline is not None else ()

    def title(self) -> str:
        title = self.w.metadata_panel.metadata().title.strip()
        if not title and self.w.preview.media is not None:
            title = Path(self.w.preview.media.path).stem.replace("_", " ")
        return title

    # ------------------------------------------------------------ preview
    def refresh(self) -> None:
        """Redraw the preview (grabbing the frame first when it changed)."""
        self.editor.set_design(self.w.project.thumbnail)
        title = self.title()
        self.editor.set_title(title)
        src = self.source()
        if src is None:
            self.image = None
            self.editor.set_image(None, "Open a clip first")
            return
        video, us, composition, sw, sh = src
        self.editor.set_scrub(self._length_us(), self._to_output(us))
        self.editor.mark_candidate(us)
        self._refresh_candidates(video, sw, sh)
        key = (str(video), us)
        if self._frame is not None and self._frame[0] == key:
            self.image = make_thumbnail(self._frame[1], composition, sw, sh, self.w.project.thumbnail, title,
                                        self._blurs(), us)
            self.editor.set_image(self.image)
            return
        if self._task is not None and self._task_key == key:
            return
        self.editor.set_image(None, "Reading the frame…")
        task = Worker(grab_frame, self.w.ffmpeg.ffmpeg, video, us)
        task.signals.finished.connect(lambda data, k=key, t=task: (self._running.discard(t), self._grabbed(k, data)))
        task.signals.failed.connect(lambda error, k=key, t=task: (self._running.discard(t), self._failed(k, error)))
        self._running.add(task)
        self._task, self._task_key = task, key
        QThreadPool.globalInstance().start(task)

    def _grabbed(self, key: tuple, data: bytes) -> None:
        if key != self._task_key:
            return
        self._task = self._task_key = None
        self._frame = (key, data)
        self.refresh()
        self.w._update_youtube_preview()

    def _failed(self, key: tuple, error) -> None:
        if key != self._task_key:
            return
        self._task = self._task_key = None
        log.warning("Thumbnail frame failed: %s", error)
        self.editor.set_image(None, "Could not read the frame")

    def pixmap(self) -> QPixmap | None:
        """The thumbnail for the YouTube tile, when it is on and vertical."""
        design = self.w.project.thumbnail
        if self.image is None or not design.enabled:
            return None
        return QPixmap.fromImage(self.image)

    # ------------------------------------------------------------ edits
    def _design_changed(self, design) -> None:
        if design != self.w.project.thumbnail:
            self.w.project.thumbnail = design
            self.w._set_dirty(True)
            self.w._remember_timer.start()  # the clip memory keeps the cover too
        self.refresh()
        self.w._update_youtube_preview()

    # ------------------------------------------------------------ suggested frames
    def _refresh_candidates(self, video, sw: int, sh: int) -> None:
        timeline = self.w.timeline.timeline
        peaks = [h.peak_us for h in sorted(self.w.project.highlights_for(str(video)) or (), key=lambda h: -h.score)
                 if timeline is None or timeline.segment_index_at(h.peak_us) is not None]
        times = candidate_times(self._length_us(), self._to_source, peaks)
        items = [(us, self._composition_at(us)) for us in times]
        blurs = self._blurs()
        key = (str(video), tuple(items), sw, sh, blurs)
        if key == self._candidates_key:
            return
        self._candidates_key = key
        self.editor.set_candidates([])
        self.editor.frames_hint.setText("Looking for good frames…")
        worker = Worker(candidate_frames, self.w.ffmpeg.ffmpeg, video, items, sw, sh, blurs=blurs)
        worker.signals.finished.connect(
            lambda frames, k=key, t=worker: (self._running.discard(t), self._candidates_ready(k, frames)))
        worker.signals.failed.connect(lambda _e, t=worker: self._running.discard(t))
        self._running.add(worker)
        QThreadPool.globalInstance().start(worker)

    def _candidates_ready(self, key: tuple, frames) -> None:
        if key == self._candidates_key:
            src = self.source()
            self.editor.set_candidates(frames, src[1] if src else None)

    def use_frame(self, source_us: int) -> None:
        design = replace(self.w.project.thumbnail, source_us=int(source_us))
        self.editor.set_design(design)
        self._design_changed(design)

    def _scrubbed(self, output_us: int) -> None:
        src = self.source()
        if src is not None and self._to_output(src[1]) // 1000 == output_us // 1000:
            return  # the slider only shows the frame already chosen
        self.use_frame(self._to_source(output_us))

    def use_playhead(self) -> None:
        design = replace(self.w.project.thumbnail, source_us=int(self.w.composition.time_us))
        self.editor.set_design(design)
        self._design_changed(design)

    # ------------------------------------------------------------ output
    def job(self):
        """A callable making the JPEG (run in the upload worker), or None when off."""
        design = self.w.project.thumbnail
        src = self.source()
        if not design.enabled or src is None:
            return None
        video, us, composition, sw, sh = src
        title, ffmpeg = self.title(), self.w.ffmpeg.ffmpeg
        cached = self._frame[1] if self._frame is not None and self._frame[0] == (str(video), us) else None
        blurs = self._blurs()

        def make() -> bytes:
            frame = cached if cached is not None else grab_frame(ffmpeg, video, us)
            return jpeg_bytes(make_thumbnail(frame, composition, sw, sh, design, title, blurs, us))

        return make

    def save_as(self) -> None:
        make = self.job()
        if make is None:
            return
        folder = self.w.config.export_dir or str(Path(self.w.preview.media.path).parent)
        name = Path(self.w.preview.media.path).stem + "_thumbnail.jpg"
        path, _ = QFileDialog.getSaveFileName(self.w, "Save thumbnail", str(Path(folder) / name),
                                              "JPEG image (*.jpg);;PNG image (*.png)")
        if not path:
            return
        try:
            if Path(path).suffix.lower() == ".png" and self.image is not None:
                save_thumbnail(self.image, Path(path))
            else:
                Path(path).write_bytes(make())
        except Exception as exc:  # noqa: BLE001 - shown to the user
            QMessageBox.warning(self.w, "Thumbnail", f"Could not save the thumbnail:\n{exc}")
            return
        self.w.statusBar().showMessage(f"Thumbnail saved: {path}", 5000)
