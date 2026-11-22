"""The Data page's thumbnail: which frame, grabbing it off the GUI thread,
the preview, saving the picture and the JPEG for the upload."""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QObject, QThreadPool
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QFileDialog, QMessageBox

from core.editor.composition import WEBCAM
from core.thumbnails.auto import AutoCoverCancelled, apply_pick, find_cover_frames, sample_times
from core.thumbnails.draw import (
    candidate_frames,
    candidate_times,
    face_in_output,
    jpeg_bytes,
    make_thumbnail,
    save_thumbnail,
)
from core.thumbnails.frame import grab_frame
from core.workers import CancellableTask, Worker

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
        self.candidates: list = []  # (source µs, small composed frame) of the suggested frames
        editor.save_requested.connect(self.save_as)
        editor.auto_requested.connect(self.auto_cover)
        editor.auto_cancel_requested.connect(self.cancel_auto)
        self.auto_task: CancellableTask | None = None
        self.auto_picks: dict[str, list] = {}  # video path → CoverPick list (best first) from Auto cover
        self.detector_factory = None  # tests inject (face detector, emotion model) factories
        self.emotion_factory = None

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
        self.w.variants_card.mark_cover(design)

    # ------------------------------------------------------------ suggested frames
    def _peaks(self, video) -> list[int]:
        """Loudest moments inside the edit, loudest first (source µs)."""
        timeline = self.w.timeline.timeline
        return [h.peak_us for h in sorted(self.w.project.highlights_for(str(video)) or (), key=lambda h: -h.score)
                if timeline is None or timeline.segment_index_at(h.peak_us) is not None]

    def _in_edit(self, us: int) -> bool:
        timeline = self.w.timeline.timeline
        return timeline is None or timeline.segment_index_at(us) is not None

    def _refresh_candidates(self, video, sw: int, sh: int) -> None:
        picks = [p for p in self.auto_picks.get(str(video), ()) if self._in_edit(p.source_us)]
        if picks:  # Auto cover ran: its best frames, best first
            times = [p.source_us for p in picks[:6]]
        else:
            times = candidate_times(self._length_us(), self._to_source, self._peaks(video))
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
            self.candidates = list(frames)
            self.w._make_cover_variants()

    def use_frame(self, source_us: int) -> None:
        self._set_frame(int(source_us))

    def _set_frame(self, us: int) -> None:
        """Another frame; the face box follows when Auto cover saw a face there."""
        old = self.w.project.thumbnail
        face = self.face_at(us)
        if face is None and old.source_us == us:
            face = old.face
        design = replace(old, source_us=us, face=face,
                         face_cutout=face is not None and (old.face_cutout or old.face is None)).clean()
        self.editor.set_design(design)
        self._design_changed(design)

    def _scrubbed(self, output_us: int) -> None:
        src = self.source()
        if src is not None and self._to_output(src[1]) // 1000 == output_us // 1000:
            return  # the slider only shows the frame already chosen
        self.use_frame(self._to_source(output_us))

    def use_playhead(self) -> None:
        self._set_frame(int(self.w.composition.time_us))

    # ------------------------------------------------------------ auto cover
    def _video_key(self) -> str:
        media = self.w.preview.media
        return str(media.path) if media is not None else ""

    def face_at(self, us: int):
        """The face Auto cover found on this frame (0..1 box of the source), or None."""
        for pick in self.auto_picks.get(self._video_key(), ()):
            if pick.source_us == us:
                return pick.face
        return None

    def faces_for(self, times) -> dict:
        design = self.w.project.thumbnail
        result = {us: self.face_at(us) for us in times}
        if design.source_us in result and result[design.source_us] is None:
            result[design.source_us] = design.face
        return {us: face for us, face in result.items() if face is not None}

    def face_boxes(self, times) -> dict:
        """Source µs → (face, webcam area) as 0..1 boxes of the composed frame."""
        src = self.source()
        if src is None:
            return {}
        _video, _us, _comp, sw, sh = src
        boxes = {}
        for us, face in self.faces_for(times).items():
            found = face_in_output(face, self._composition_at(us), sw, sh, with_area=True)
            if found is not None:
                boxes[us] = found
        return boxes

    def _factories(self):
        models = self.w.config.data_dir / "models"
        detector = self.detector_factory
        emotion = self.emotion_factory
        if detector is None:
            from core.vision.faces import FaceDetector, download_model

            detector = lambda: FaceDetector(download_model(models))  # noqa: E731
        if emotion is None:
            from core.vision.emotion import EmotionModel, download_model as download_emotion

            emotion = lambda: EmotionModel(download_emotion(models))  # noqa: E731
        return detector, emotion

    def auto_cover(self) -> bool:
        """Find the frame with the strongest emotion on the streamer's face and set the cover up."""
        if self.auto_task is not None:
            return False
        media = self.w.preview.media
        if media is None or not media.has_video or self.w.ffmpeg is None or self._length_us() <= 0:
            self.editor.set_auto_busy(False, "Open a clip first.")
            return False
        video = Path(media.path)
        sw, sh = self.w.composition.source_size
        peaks = self._peaks(video)
        times = [us for us in sample_times(self._length_us(), self._to_source, peaks) if self._in_edit(us)]
        if not times:
            self.editor.set_auto_busy(False, "The Short is empty.")
            return False
        mid = times[len(times) // 2]
        webcam = self._composition_at(mid).webcam.crop
        detector, emotion = self._factories()
        task = CancellableTask(find_cover_frames, self.w.ffmpeg.ffmpeg, video, times, webcam, sw, sh,
                               detector, emotion, peaks)
        task.signals.progress.connect(lambda f, m: self.editor.set_auto_busy(True, f"{m} {f * 100:.0f}%"))
        task.signals.finished.connect(lambda result, v=str(video): self._auto_done(v, result))
        task.signals.failed.connect(self._auto_failed)
        task.signals.cancelled.connect(lambda: self._auto_finish("Cancelled."))
        self.auto_task = task
        self.editor.set_auto_busy(True, "Starting…")
        QThreadPool.globalInstance().start(task)
        return True

    def cancel_auto(self) -> None:
        if self.auto_task is not None:
            self.auto_task.cancel()

    def _auto_finish(self, message: str) -> None:
        self.auto_task = None
        self.editor.set_auto_busy(False, message)

    def _auto_failed(self, error) -> None:
        if isinstance(error, AutoCoverCancelled):
            self._auto_finish("Cancelled.")
            return
        log.warning("Auto cover failed: %s", error)
        self._auto_finish(f"Auto cover failed: {error}")

    def _auto_done(self, video: str, result) -> None:
        if self._video_key() != video:
            self._auto_finish("The clip changed; nothing was applied.")
            return
        self.auto_task = None
        best = result.best
        if best is None:
            self.editor.set_auto_busy(False, "No frames could be read.")
            return
        self.auto_picks[video] = list(result.picks)
        src = self.source()
        sw, sh = (src[3], src[4]) if src else self.w.composition.source_size
        box = face_in_output(best.face, self._composition_at(best.source_us), sw, sh)
        design = apply_pick(self.w.project.thumbnail, best, box.center().y() if box is not None else None)
        self.editor.set_design(design)
        self._candidates_key = None  # suggested frames become the best ones found
        self._design_changed(design)
        at = self._to_output(best.source_us) / 1e6
        if best.face is None:
            note = "No face found in the webcam; picked the loudest clear frame."
        elif result.with_emotions:
            note = f"Picked the frame at {int(at // 60)}:{at % 60:04.1f} · {best.note}."
        else:
            note = (f"Picked the frame at {int(at // 60)}:{at % 60:04.1f} by face size and loudness "
                    "(the emotion model could not be loaded).")
        self.editor.set_auto_busy(False, note + f" Looked at {result.sampled} frames; the best ones are under Suggested.")
        self.w.statusBar().showMessage("Auto cover ready", 5000)

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
