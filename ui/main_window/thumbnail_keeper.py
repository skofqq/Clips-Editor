"""The Data page's thumbnail: which frame, grabbing it off the GUI thread,
the preview, saving the picture and the JPEG for the upload."""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QObject, QThreadPool
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QFileDialog, QMessageBox

from core.thumbnails.draw import jpeg_bytes, make_thumbnail, save_thumbnail
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
        editor.save_requested.connect(self.save_as)

    # ------------------------------------------------------------ source
    def source(self):
        """(video, source_us, composition, src_w, src_h) of the chosen frame, or None."""
        media = self.w.preview.media
        if media is None or not media.has_video or self.w.ffmpeg is None:
            return None
        design = self.w.project.thumbnail
        timeline = self.w.timeline.timeline
        us = design.source_us
        if us is None:
            if timeline is not None:
                us = timeline.output_to_source(timeline.output_us // 4)
            else:
                us = int(media.duration_s * 1e6 / 4)
        layout = None
        if timeline is not None and timeline.segments:
            layout = timeline.segments[timeline.layout_index_at(us)].layout
        composition = (layout or self.w.main_layout).at(us)
        src_w, src_h = self.w.composition.source_size
        return media.path, int(us), composition, src_w, src_h

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
        key = (str(video), us)
        if self._frame is not None and self._frame[0] == key:
            self.image = make_thumbnail(self._frame[1], composition, sw, sh, self.w.project.thumbnail, title)
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
        self.refresh()
        self.w._update_youtube_preview()

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

        def make() -> bytes:
            frame = cached if cached is not None else grab_frame(ffmpeg, video, us)
            return jpeg_bytes(make_thumbnail(frame, composition, sw, sh, design, title))

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
