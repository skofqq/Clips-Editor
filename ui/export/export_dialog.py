"""Export dialog: choose settings, render in the background, show progress."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QThreadPool, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from core.editor.composition import Composition
from core.media.ffmpeg_locator import FFmpegPaths
from core.media.probe import MediaInfo
from core.render.encoders import EncoderInfo, choose_encoder, encoder_info
from core.render.graph import output_fps
from core.render.job import RenderError, render
from core.audio.enhance import LOUDNESS_CHOICES, LOUDNESS_OFF
from core.render.settings import (
    AAC,
    AUTO,
    BITRATE,
    CPU,
    CUSTOM_PRESET,
    FPS_CHOICES,
    H264,
    HEVC,
    MKV,
    MP4,
    OPUS,
    PRESETS,
    QUALITY,
    RESOLUTIONS,
    SOURCE_FPS,
    RenderSettings,
    preset_name,
)
from core.timeline.model import Timeline
from core.timeline.timebase import format_timecode
from core.workers import CancellableTask
from ui.widgets.card import Card

log = logging.getLogger(__name__)

AUDIO_BITRATES = (128, 160, 192, 256, 320)


def default_output_path(source: Path, folder: str, extension: str) -> Path:
    """``<folder>/<source name>_short.mp4``, numbered if the file exists."""
    base = Path(folder) if folder and Path(folder).is_dir() else source.parent
    candidate = base / f"{source.stem}_short{extension}"
    number = 2
    while candidate.exists():
        candidate = base / f"{source.stem}_short_{number}{extension}"
        number += 1
    return candidate


class ExportDialog(QDialog):
    """Export settings, progress and result. Shown inside the Render page
    (:meth:`embed`) or, for scripts and tests, as a dialog."""

    rendered = Signal(object)  # Path of a finished export
    queued = Signal(object)  # RenderJob to add to the render queue

    def __init__(
        self,
        source: MediaInfo,
        composition: Composition,
        timeline: Timeline,
        ffmpeg: FFmpegPaths,
        settings: RenderSettings,
        output: Path,
        encoders: list[EncoderInfo] | None = None,
        parent: QWidget | None = None,
        upload=None,  # callable(path) opening the YouTube upload dialog
        captions: list | None = None,  # subtitles (output time) to burn in
        subtitle_style=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export Video")
        self.setMinimumWidth(600)
        self.source = source
        self.composition = composition
        self.timeline = timeline
        self.ffmpeg = ffmpeg
        self.encoders = encoders
        self.result_path: Path | None = None
        self.upload = upload
        self.captions = list(captions or [])
        self.subtitle_style = subtitle_style
        self._task: CancellableTask | None = None
        self._close_when_stopped = False
        self._loading = False

        # ------------------------------------------------------ controls
        self.preset_combo = QComboBox(self)
        for name in PRESETS:
            self.preset_combo.addItem(name, name)
        self.preset_combo.addItem(CUSTOM_PRESET, CUSTOM_PRESET)
        self.resolution_combo = QComboBox(self)
        for label, w, h in RESOLUTIONS:
            self.resolution_combo.addItem(label, f"{w}x{h}")
        self.resolution_combo.addItem("Custom", "")
        self.resolution_combo.setMinimumContentsLength(11)
        self.resolution_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.width_spin = self._spin(16, 4096, 2, " px")
        self.height_spin = self._spin(16, 4096, 2, " px")
        self.fps_combo = QComboBox(self)
        fps = output_fps(RenderSettings(), source)
        self.fps_combo.addItem(f"Same as source ({float(fps):g})", SOURCE_FPS)
        for value in FPS_CHOICES:
            self.fps_combo.addItem(str(value), value)
        self.codec_combo = QComboBox(self)
        self.codec_combo.addItem("H.264 (best compatibility)", H264)
        self.codec_combo.addItem("H.265 / HEVC (smaller files)", HEVC)
        self.encoder_combo = QComboBox(self)
        self.rate_combo = QComboBox(self)
        self.rate_combo.addItem("Constant quality (CRF / CQ)", QUALITY)
        self.rate_combo.addItem("Target bitrate", BITRATE)
        self.quality_spin = self._spin(0, 51, 1, "")
        self.quality_spin.setToolTip("Lower = better quality and bigger file. 18–23 is a good range.")
        self.bitrate_spin = self._spin(500, 200_000, 500, " kbps")
        self.container_combo = QComboBox(self)
        self.container_combo.addItem("MP4", MP4)
        self.container_combo.addItem("MKV", MKV)
        self.audio_combo = QComboBox(self)
        self.audio_combo.addItem("AAC", AAC)
        self.audio_combo.addItem("Opus", OPUS)
        self.audio_bitrate_combo = QComboBox(self)
        for value in AUDIO_BITRATES:
            self.audio_bitrate_combo.addItem(f"{value} kbps", value)
        self.beep_spin = self._spin(200, 4000, 50, " Hz")
        self.beep_spin.setToolTip("Tone used for Mute Regions set to Beep")
        self.loudness_combo = QComboBox(self)
        for value, label in LOUDNESS_CHOICES:
            self.loudness_combo.addItem(label, value)
        self.loudness_combo.addItem("Off (keep the source level)", LOUDNESS_OFF)
        self.loudness_combo.setToolTip(
            "Measures the finished sound and sets one even level for the whole Short, with peaks kept "
            "under -1 dB. YouTube turns louder videos down to -14 LUFS anyway, quieter ones stay quiet.")
        # Folder and file name are separate fields; the extension follows the container.
        output = Path(output)
        self.folder_edit = QLineEdit(str(output.parent) if str(output) else "", self)
        self.folder_edit.setPlaceholderText("Folder")
        self.name_edit = QLineEdit(output.stem, self)
        self.name_edit.setPlaceholderText("File name")
        self.extension_label = QLabel(self)
        self.extension_label.setObjectName("muted")
        self.browse_button = QPushButton("Browse…", self)
        self.browse_button.setToolTip("Choose the folder")

        self.summary_label = QLabel(self)
        self.summary_label.setObjectName("muted")
        self.summary_label.setWordWrap(True)
        self.progress = QProgressBar(self)
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(8)
        self.status_label = QLabel(self)
        self.status_label.setWordWrap(True)
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        self.render_button = QPushButton("Render", self)
        self.render_button.setObjectName("primary")
        self.render_button.setDefault(True)
        self.cancel_button = QPushButton("Cancel render", self)
        self.open_button = QPushButton("Show in folder", self)
        self.close_button = QPushButton("Close", self)
        self.upload_button = QPushButton("Upload to YouTube…", self)
        self.queue_button = QPushButton("Add to queue", self)
        self.queue_button.setToolTip("Render later, one after another, while you edit the next clip")
        self.queue_button.hide()  # shown where a queue exists (the Render page)
        self.upload_check = QCheckBox("Upload to YouTube when rendered", self)
        self.upload_check.setToolTip(
            "For queued jobs: publish right after rendering with the Data page's title, description, tags and "
            "thumbnail, on the active channel, with the last visibility and category from the Publish page")
        self.upload_check.hide()

        # ------------------------------------------------------ layout
        video_card = Card("Video", parent=self, label_width=96)
        video_card.add_row("Preset", self.preset_combo)
        size = QHBoxLayout()
        size.addWidget(self.resolution_combo, 3)
        size.addWidget(self.width_spin, 1)
        size.addWidget(QLabel("×", self))
        size.addWidget(self.height_spin, 1)
        video_card.add_row("Resolution", size)
        video_card.add_row("Frame rate", self.fps_combo)
        video_card.add_row("Codec", self.codec_combo)
        video_card.add_row("Encoder", self.encoder_combo)
        rate = QHBoxLayout()
        rate.addWidget(self.rate_combo, 2)
        rate.addWidget(self.quality_spin, 1)
        rate.addWidget(self.bitrate_spin, 1)
        video_card.add_row("Quality", rate)

        self.subtitles_check = QCheckBox("Burn subtitles into the video", self)
        has_subs = bool(self.captions) and subtitle_style is not None
        self.subtitles_check.setChecked(has_subs and subtitle_style.enabled)
        self.subtitles_check.setEnabled(has_subs)
        self.subtitles_check.setToolTip(
            "Draws the captions from the Subtitles tab on the picture" if has_subs
            else "No subtitles: transcribe the clip and turn them on in the Subtitles tab"
        )
        video_card.add_row("Subtitles", self.subtitles_check)

        audio_card = Card("Audio", parent=self, label_width=96)
        audio = QHBoxLayout()
        audio.addWidget(self.audio_combo, 1)
        audio.addWidget(self.audio_bitrate_combo, 1)
        audio_card.add_row("Track (48 kHz)", audio)
        audio_card.add_row("Loudness", self.loudness_combo)
        audio_card.add_row("Beep tone", self.beep_spin)

        file_card = Card("File", parent=self, label_width=96)
        file_card.add_row("Container", self.container_combo)
        folder = QHBoxLayout()
        folder.addWidget(self.folder_edit, 1)
        folder.addWidget(self.browse_button)
        file_card.add_row("Folder", folder)
        name = QHBoxLayout()
        name.addWidget(self.name_edit, 1)
        name.addWidget(self.extension_label)
        file_card.add_row("File name", name)

        buttons = QHBoxLayout()
        buttons.addWidget(self.open_button)
        buttons.addWidget(self.upload_button)
        buttons.addStretch(1)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.upload_check)
        buttons.addWidget(self.queue_button)
        buttons.addWidget(self.render_button)
        buttons.addWidget(self.close_button)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        for card in (video_card, audio_card, file_card):
            layout.addWidget(card)
        layout.addWidget(self.summary_label)
        layout.addWidget(self.progress)
        layout.addWidget(self.status_label)
        layout.addLayout(buttons)

        # ------------------------------------------------------ wiring
        self.preset_combo.currentIndexChanged.connect(self._preset_chosen)
        self.resolution_combo.currentIndexChanged.connect(self._resolution_chosen)
        self.codec_combo.currentIndexChanged.connect(self._codec_changed)
        self.rate_combo.currentIndexChanged.connect(self._update_rate_widgets)
        for widget in (self.fps_combo, self.encoder_combo, self.rate_combo, self.container_combo,
                       self.audio_combo, self.audio_bitrate_combo, self.loudness_combo):
            widget.currentIndexChanged.connect(self._edited)
        for spin in (self.width_spin, self.height_spin, self.quality_spin, self.bitrate_spin, self.beep_spin):
            spin.valueChanged.connect(self._edited)
        self.container_combo.currentIndexChanged.connect(self._fix_extension)
        self.browse_button.clicked.connect(self._browse)
        self.render_button.clicked.connect(self.start_render)
        self.cancel_button.clicked.connect(self.cancel_render)
        self.open_button.clicked.connect(self._show_in_folder)
        self.close_button.clicked.connect(self.reject)
        self.upload_button.clicked.connect(self._upload)
        self.queue_button.clicked.connect(self.add_to_queue)

        self.set_settings(settings)
        self._set_busy(False)
        self.open_button.setVisible(False)
        self.upload_button.setVisible(False)

    # ------------------------------------------------------------ helpers
    def _spin(self, low: int, high: int, step: int, suffix: str) -> QSpinBox:
        spin = QSpinBox(self)
        spin.setRange(low, high)
        spin.setSingleStep(step)
        spin.setSuffix(suffix)
        return spin

    @staticmethod
    def _select(combo: QComboBox, data) -> None:
        index = combo.findData(data)
        if index >= 0:
            combo.setCurrentIndex(index)

    # ------------------------------------------------------------ settings
    def settings(self) -> RenderSettings:
        return RenderSettings(
            width=self.width_spin.value(),
            height=self.height_spin.value(),
            fps=self.fps_combo.currentData(),
            video_codec=self.codec_combo.currentData(),
            container=self.container_combo.currentData(),
            audio_codec=self.audio_combo.currentData(),
            audio_bitrate_k=self.audio_bitrate_combo.currentData(),
            rate_control=self.rate_combo.currentData(),
            quality=self.quality_spin.value(),
            video_bitrate_k=self.bitrate_spin.value(),
            encoder=self.encoder_combo.currentData() or AUTO,
            beep_hz=self.beep_spin.value(),
            loudness_lufs=self.loudness_combo.currentData(),
        ).cleaned()

    def set_settings(self, settings: RenderSettings) -> None:
        settings = settings.cleaned()
        self._loading = True
        try:
            self.width_spin.setValue(settings.width)
            self.height_spin.setValue(settings.height)
            index = self.resolution_combo.findData(f"{settings.width}x{settings.height}")
            self.resolution_combo.setCurrentIndex(index if index >= 0 else self.resolution_combo.count() - 1)
            self._select(self.fps_combo, settings.fps)
            self._select(self.codec_combo, settings.video_codec)
            self._fill_encoders(settings.encoder)
            self._select(self.rate_combo, settings.rate_control)
            self.quality_spin.setValue(settings.quality)
            self.bitrate_spin.setValue(settings.video_bitrate_k)
            self._select(self.container_combo, settings.container)
            self._select(self.audio_combo, settings.audio_codec)
            self._select(self.audio_bitrate_combo, settings.audio_bitrate_k)
            self.beep_spin.setValue(settings.beep_hz)
            index = self.loudness_combo.findData(settings.loudness_lufs)
            self.loudness_combo.setCurrentIndex(index if index >= 0 else 0)
            self._select(self.preset_combo, preset_name(settings))
        finally:
            self._loading = False
        self._update_rate_widgets()
        self._update_size_widgets()
        self._fix_extension()
        self._update_summary()

    def set_encoders(self, encoders: list[EncoderInfo] | None) -> None:
        self.encoders = encoders
        self._loading = True
        try:
            self._fill_encoders(self.encoder_combo.currentData() or AUTO)
        finally:
            self._loading = False
        self._update_summary()

    def _fill_encoders(self, wanted: str) -> None:
        codec = self.codec_combo.currentData()
        self.encoder_combo.clear()
        if self.encoders is None:
            self.encoder_combo.addItem("Auto (detecting encoders…)", AUTO)
        else:
            best = encoder_info(choose_encoder(RenderSettings(video_codec=codec), self.encoders))
            self.encoder_combo.addItem(f"Auto: {best.label}", AUTO)
            for info in self.encoders:
                if info.codec == codec and info.is_hardware:
                    self.encoder_combo.addItem(info.label, info.name)
        self.encoder_combo.addItem("CPU (software)", CPU)
        index = self.encoder_combo.findData(wanted)
        self.encoder_combo.setCurrentIndex(max(0, index))

    def _preset_chosen(self) -> None:
        if self._loading:
            return
        name = self.preset_combo.currentData()
        preset = PRESETS.get(name)
        if preset is None:
            return
        current = self.settings()
        # Keep the size, frame rate, encoder choice and beep; take the rest.
        self.set_settings(RenderSettings(
            **{**preset.to_dict(), "width": current.width, "height": current.height, "fps": current.fps,
               "encoder": AUTO if current.encoder not in (AUTO, CPU) else current.encoder,
               "beep_hz": current.beep_hz, "loudness_lufs": current.loudness_lufs}
        ))

    def _resolution_chosen(self) -> None:
        size = self.resolution_combo.currentData()
        if size and not self._loading:
            w, h = (int(v) for v in size.split("x"))
            self._loading = True
            try:
                self.width_spin.setValue(w)
                self.height_spin.setValue(h)
            finally:
                self._loading = False
        self._update_size_widgets()
        self._edited()

    def _update_size_widgets(self) -> None:
        custom = not self.resolution_combo.currentData()
        self.width_spin.setEnabled(custom)
        self.height_spin.setEnabled(custom)

    def _codec_changed(self) -> None:
        if self._loading:
            return
        self._loading = True
        try:
            self._fill_encoders(self.encoder_combo.currentData() or AUTO)
        finally:
            self._loading = False
        self._edited()

    def _update_rate_widgets(self) -> None:
        by_quality = self.rate_combo.currentData() == QUALITY
        self.quality_spin.setVisible(by_quality)
        self.bitrate_spin.setVisible(not by_quality)

    def _edited(self) -> None:
        if self._loading:
            return
        self._loading = True
        try:
            self._select(self.preset_combo, preset_name(self.settings()))
        finally:
            self._loading = False
        self._update_summary()

    def _fix_extension(self) -> None:
        self.extension_label.setText("." + self.container_combo.currentData())

    def _update_summary(self) -> None:
        s = self.settings()
        encoder = encoder_info(choose_encoder(s, self.encoders))
        fps = output_fps(s, self.source)
        cuts = len(self.timeline.segments)
        mutes = len(self.timeline.merged_mutes())
        text = (
            f"Output {format_timecode(self.timeline.output_us)} · {s.width}×{s.height} · {float(fps):g} fps · "
            f"{encoder.label}"
        )
        details = []
        if cuts > 1:
            details.append(f"{cuts} kept parts")
        if mutes:
            details.append(f"{mutes} mute region{'s' if mutes != 1 else ''}")
        if (s.width, s.height) != (self.composition.output_w, self.composition.output_h):
            details.append(
                f"layout is {self.composition.output_w}×{self.composition.output_h}: "
                "it is scaled to fit and padded with black"
            )
        if details:
            text += "\n" + " · ".join(details)
        self.summary_label.setText(text)

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Export Video: folder", self.folder_edit.text().strip())
        if folder:
            self.folder_edit.setText(str(Path(folder)))

    # ------------------------------------------------------------ render
    def file_name(self) -> str:
        """The name field without an extension the user may have typed, and without characters
        Windows does not allow in file names."""
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", self.name_edit.text()).strip().rstrip(".")
        for ext in (".mp4", ".mkv"):
            if name.lower().endswith(ext):
                name = name[: -len(ext)].rstrip()
        return name

    def output_path(self) -> Path:
        return Path(self.folder_edit.text().strip()) / (self.file_name() + self.settings().extension)

    def _set_busy(self, busy: bool) -> None:
        for widget in (*self.findChildren(QComboBox), *self.findChildren(QSpinBox), self.folder_edit,
                       self.name_edit):
            widget.setEnabled(not busy)
        self.browse_button.setEnabled(not busy)
        if not busy:
            self._update_size_widgets()
        self.render_button.setVisible(not busy)
        self.cancel_button.setVisible(busy)
        self.close_button.setEnabled(not busy)
        self.progress.setVisible(busy)

    def _checked_output(self) -> Path | None:
        """The output path after the usual checks (asks before replacing a file); None to stop."""
        if not self.folder_edit.text().strip():
            QMessageBox.warning(self, "Export Video", "Choose the folder to save the video in.")
            return None
        if not self.file_name():
            QMessageBox.warning(self, "Export Video", "Type a file name.")
            return None
        output = self.output_path()
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, "Export Video", f"Cannot use this folder: {exc}")
            return None
        if output.resolve() == Path(self.source.path).resolve():
            QMessageBox.warning(self, "Export Video", "Choose a different file: this is the source video.")
            return None
        if output.exists():
            answer = QMessageBox.question(self, "Export Video", f"{output.name} already exists. Replace it?")
            if answer != QMessageBox.StandardButton.Yes:
                return None
        return output

    def add_to_queue(self) -> None:
        from ui.export.render_queue import RenderJob

        output = self._checked_output()
        if output is None:
            return
        job = RenderJob(self.source, self.composition, self.timeline, self.settings(), output,
                        self.encoders, self._subtitle_args())
        self.queued.emit(job)
        self.status_label.setText(f"Added to the queue: {output.name}")

    def start_render(self) -> None:
        if self._task is not None:
            return
        output = self._checked_output()
        if output is None:
            return
        settings = self.settings()
        task = CancellableTask(
            render, self.source, self.composition, self.timeline, settings, output,
            self.ffmpeg.ffmpeg, self.ffmpeg.ffprobe, self.encoders,
            **self._subtitle_args(),
        )
        task.signals.progress.connect(self._on_progress)
        task.signals.finished.connect(self._on_finished)
        task.signals.failed.connect(self._on_failed)
        task.signals.cancelled.connect(self._on_cancelled)
        self._task = task
        self.result_path = None
        self.open_button.setVisible(False)
        self.upload_button.setVisible(False)
        self.progress.setValue(0)
        self.status_label.setText(f"Rendering to {output}…")
        self._set_busy(True)
        log.info("Export %s -> %s with %s", self.source.path, output, settings)
        QThreadPool.globalInstance().start(task)

    def _subtitle_args(self) -> dict:
        if not (self.subtitles_check.isChecked() and self.captions and self.subtitle_style is not None):
            return {}
        from dataclasses import replace

        return {"captions": self.captions, "subtitle_style": replace(self.subtitle_style, enabled=True)}

    def cancel_render(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self.status_label.setText("Cancelling…")

    def _on_progress(self, fraction: float, message: str) -> None:
        self.progress.setValue(int(fraction * 1000))
        self.status_label.setText(f"{message}\n{self.output_path()}")

    def _finish(self) -> None:
        self._task = None
        self._set_busy(False)
        if self._close_when_stopped:
            super().reject()

    def embed(self) -> None:
        """Live inside a page instead of a window: no title bar, no Close."""
        self.setWindowFlags(Qt.WindowType.Widget)
        self.setMinimumWidth(0)
        self.close_button.hide()

    @property
    def busy(self) -> bool:
        return self._task is not None

    def set_inputs(self, composition, timeline, captions=None, subtitle_style=None) -> None:
        """The edit changed while the page is open (same source clip)."""
        if self.busy:
            return
        self.composition, self.timeline = composition, timeline
        self.captions = list(captions or [])
        self.subtitle_style = subtitle_style
        if hasattr(self, "subtitles_check"):
            self.subtitles_check.setEnabled(bool(self.captions))
        self._update_summary()

    def _on_finished(self, result) -> None:
        self.result_path = result.path
        size_mb = result.size_bytes / 1_048_576
        text = (
            f"Done in {result.elapsed_s:.0f} s: {result.path.name} · {size_mb:.1f} MB · "
            f"{format_timecode(round(result.duration_s * 1e6))} · {encoder_info(result.encoder).label}"
        )
        if result.notice:
            text += "\n" + result.notice
        self.status_label.setText(text)
        self.open_button.setVisible(True)
        self.upload_button.setVisible(self.upload is not None)
        self._finish()
        self.rendered.emit(result.path)

    def _on_failed(self, error) -> None:
        message = str(error) if isinstance(error, RenderError) else f"Unexpected error: {error}"
        log.error("Render failed: %s", message)
        self.status_label.setText("Render failed. Nothing was saved.")
        self._finish()
        if not self._close_when_stopped:
            QMessageBox.warning(self, "Render failed", message)

    def _on_cancelled(self) -> None:
        self.status_label.setText("Cancelled. The unfinished file was removed.")
        self._finish()

    def _upload(self) -> None:
        if self.upload is not None and self.result_path is not None:
            self.upload(self.result_path)

    def _show_in_folder(self) -> None:
        path = self.result_path
        if path is None:
            return
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])  # noqa: S603,S607
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))

    # ------------------------------------------------------------ closing
    def reject(self) -> None:
        if self._task is not None:
            answer = QMessageBox.question(self, "Export Video", "Stop rendering? The unfinished file is removed.")
            if answer != QMessageBox.StandardButton.Yes:
                return
            self._close_when_stopped = True
            self.cancel_render()
            return
        super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._task is not None:
            event.ignore()
            self.reject()
            return
        super().closeEvent(event)
