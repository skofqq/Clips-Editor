"""Center panel: original and vertical output previews with transport controls."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoFrame, QVideoSink
from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from core.media.probe import MediaInfo, format_duration
from ui.editor.composition_controller import CompositionController
from ui.preview.output_view import OutputView
from ui.preview.source_view import SourceView
from ui.theme import icon, palette

VIEW_MODES = ("original", "both", "output")

log = logging.getLogger(__name__)


class PreviewPanel(QWidget):
    position_changed = Signal(int)  # milliseconds

    def __init__(
        self, controller: CompositionController | None = None, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.controller = controller or CompositionController(self)
        self._view_mode = "both"
        self._media: MediaInfo | None = None
        self._frame_ms = 33
        self._duration_ms = 0
        self._was_playing_before_scrub = False
        self._audio_factor = 1.0  # timeline gain / mute / fade, 0..1

        self.source_view = SourceView(self.controller, self)
        self.output_view = OutputView(self.controller, self)
        self.canvas = self.source_view  # original frame view
        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.addWidget(self.source_view)
        self.splitter.addWidget(self.output_view)
        self.splitter.setStretchFactor(0, 2)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([640, 340])
        self.splitter.setChildrenCollapsible(False)

        self._mode_group = QButtonGroup(self)
        self._mode_group.setExclusive(True)
        mode_bar = QHBoxLayout()
        mode_bar.setContentsMargins(8, 8, 8, 6)
        mode_bar.addStretch(1)
        segmented = QWidget(self)
        segmented.setObjectName("segmented")
        segmented.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        seg_layout = QHBoxLayout(segmented)
        seg_layout.setContentsMargins(3, 3, 3, 3)
        seg_layout.setSpacing(2)
        for mode, text in zip(VIEW_MODES, ("Original", "Both", "Output 9:16")):
            button = QPushButton(text, segmented)
            button.setCheckable(True)
            button.setChecked(mode == self._view_mode)
            button.setProperty("mode", mode)
            self._mode_group.addButton(button)
            seg_layout.addWidget(button)
        mode_bar.addWidget(segmented)
        mode_bar.addStretch(1)
        self._mode_group.buttonClicked.connect(lambda b: self.set_view_mode(b.property("mode")))

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setVolume(0.8)
        self.sink = QVideoSink(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoSink(self.sink)

        self.play_button = QPushButton(self)
        self.play_button.setObjectName("playButton")
        self.play_button.setIcon(icon("play", palette.accent().on_fill))
        self.play_button.setIconSize(QSize(18, 18))
        self.play_button.setToolTip("Play / Pause (Space)")
        self.prev_frame_button = QPushButton(self)
        self.prev_frame_button.setObjectName("transportButton")
        self.prev_frame_button.setIcon(icon("prev"))
        self.prev_frame_button.setToolTip("Previous frame (Left)")
        self.next_frame_button = QPushButton(self)
        self.next_frame_button.setObjectName("transportButton")
        self.next_frame_button.setIcon(icon("next"))
        self.next_frame_button.setToolTip("Next frame (Right)")

        self.slider = QSlider(Qt.Orientation.Horizontal, self)
        self.slider.setRange(0, 0)
        self.time_label = QLabel("00:00.000 / 00:00.000", self)
        self.time_label.setObjectName("timecode")

        self.volume = QSlider(Qt.Orientation.Horizontal, self)
        self.volume.setRange(0, 100)
        self.volume.setValue(80)
        self.volume.setFixedWidth(90)
        self.volume.setToolTip("Preview volume")

        controls = QHBoxLayout()
        controls.setContentsMargins(12, 6, 12, 8)
        controls.setSpacing(6)
        controls.addWidget(self.prev_frame_button)
        controls.addWidget(self.play_button)
        controls.addWidget(self.next_frame_button)
        controls.addWidget(self.slider, 1)
        controls.addWidget(self.time_label)
        controls.addSpacing(10)
        volume_icon = QLabel(self)
        volume_icon.setPixmap(icon("volume", palette.TEXT_SECONDARY).pixmap(18, 18))
        volume_icon.setToolTip("Preview volume")
        controls.addWidget(volume_icon)
        controls.addWidget(self.volume)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(mode_bar)
        layout.addWidget(self.splitter, 1)
        layout.addLayout(controls)

        self.sink.videoFrameChanged.connect(self._on_frame)
        self.player.durationChanged.connect(self._on_duration)
        self.player.positionChanged.connect(self._on_position)
        self.player.playbackStateChanged.connect(self._on_state)
        self.player.errorOccurred.connect(self._on_error)
        self.play_button.clicked.connect(self.toggle_play)
        self.prev_frame_button.clicked.connect(lambda: self.step_frames(-1))
        self.next_frame_button.clicked.connect(lambda: self.step_frames(1))
        self.slider.sliderPressed.connect(self._on_scrub_start)
        self.slider.sliderMoved.connect(self.seek)
        self.slider.sliderReleased.connect(self._on_scrub_end)
        self.volume.valueChanged.connect(lambda _v: self._apply_volume())

        self._set_enabled(False)

    # ----------------------------------------------------------------- API
    @property
    def media(self) -> MediaInfo | None:
        return self._media

    @property
    def view_mode(self) -> str:
        return self._view_mode

    def set_view_mode(self, mode: str) -> None:
        if mode not in VIEW_MODES:
            return
        self._view_mode = mode
        self.source_view.setVisible(mode in ("original", "both"))
        self.output_view.setVisible(mode in ("output", "both"))
        for button in self._mode_group.buttons():
            button.setChecked(button.property("mode") == mode)

    def load(self, media: MediaInfo) -> None:
        self._media = media
        if media.video:
            self.controller.set_source_size(*media.video.display_size)
        fps = media.video.avg_fps or media.video.fps if media.video else None
        self._frame_ms = max(1, round(1000 / float(fps))) if fps else 33
        self.canvas.clear("Loading…")
        self.player.setSource(QUrl.fromLocalFile(str(Path(media.path))))
        self._set_enabled(True)
        # Show the first frame without starting playback.
        self.player.pause()
        log.info("Preview loaded %s", media.path)

    def unload(self) -> None:
        self.player.stop()
        self.player.setSource(QUrl())
        self._media = None
        self._duration_ms = 0
        self.slider.setRange(0, 0)
        self._update_time(0)
        self.canvas.clear("Import a video to start (File → Import or drag and drop)")
        self.output_view.clear()
        self._set_enabled(False)

    def set_audio_factor(self, factor: float) -> None:
        """Extra volume multiplier from the edit (mutes, gain, fades)."""
        factor = max(0.0, min(1.0, float(factor)))
        if abs(factor - self._audio_factor) > 1e-4:
            self._audio_factor = factor
            self._apply_volume()

    def _apply_volume(self) -> None:
        self.audio.setVolume(self.volume.value() / 100 * self._audio_factor)

    def toggle_play(self) -> None:
        if self._media is None:
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            if self._duration_ms and self.player.position() >= self._duration_ms - self._frame_ms:
                self.player.setPosition(0)
            self.player.play()

    @property
    def is_playing(self) -> bool:
        return self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    def play(self) -> None:
        if self._media is not None:
            self.player.play()

    def pause(self) -> None:
        if self._media is not None:
            self.player.pause()

    def seek(self, ms: int) -> None:
        if self._media is None:
            return
        ms = max(0, min(int(ms), self._duration_ms or int(ms)))
        self.player.setPosition(ms)
        self._update_time(ms)

    def step_frames(self, count: int) -> None:
        if self._media is None:
            return
        self.player.pause()
        self.seek(self.player.position() + count * self._frame_ms)

    # ------------------------------------------------------------ handlers
    def _on_frame(self, frame: QVideoFrame) -> None:
        if frame.isValid():
            image = frame.toImage()
            self.source_view.set_image(image)
            self.output_view.set_image(image)

    def _on_duration(self, duration: int) -> None:
        self._duration_ms = max(0, duration)
        self.slider.setRange(0, self._duration_ms)
        self._update_time(self.player.position())

    def _on_position(self, position: int) -> None:
        if not self.slider.isSliderDown():
            self.slider.setValue(position)
        self._update_time(position)
        self.position_changed.emit(position)

    def _on_state(self, state: QMediaPlayer.PlaybackState) -> None:
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self.play_button.setIcon(icon("pause" if playing else "play", palette.accent().on_fill))

    def _on_error(self, error, message: str) -> None:
        if error != QMediaPlayer.Error.NoError:
            log.error("Preview error: %s", message)
            self.canvas.clear(f"Preview unavailable: {message}")

    def _on_scrub_start(self) -> None:
        self._was_playing_before_scrub = (
            self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        )
        self.player.pause()

    def _on_scrub_end(self) -> None:
        self.seek(self.slider.value())
        if self._was_playing_before_scrub:
            self.player.play()

    def _update_time(self, position_ms: int) -> None:
        self.time_label.setText(
            f"{format_duration(position_ms / 1000)} / {format_duration(self._duration_ms / 1000)}"
        )

    def _set_enabled(self, enabled: bool) -> None:
        for widget in (self.play_button, self.prev_frame_button, self.next_frame_button, self.slider):
            widget.setEnabled(enabled)
