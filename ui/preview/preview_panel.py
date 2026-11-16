"""Center panel: original video preview with transport controls."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoFrame, QVideoSink
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from core.media.probe import MediaInfo, format_duration
from ui.preview.video_canvas import VideoCanvas

log = logging.getLogger(__name__)


class PreviewPanel(QWidget):
    position_changed = Signal(int)  # milliseconds

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._media: MediaInfo | None = None
        self._frame_ms = 33
        self._duration_ms = 0
        self._was_playing_before_scrub = False

        self.canvas = VideoCanvas(self)

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setVolume(0.8)
        self.sink = QVideoSink(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoSink(self.sink)

        style = self.style()
        self.play_button = QPushButton(self)
        self.play_button.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.play_button.setToolTip("Play / Pause (Space)")
        self.prev_frame_button = QPushButton(self)
        self.prev_frame_button.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_MediaSeekBackward))
        self.prev_frame_button.setToolTip("Previous frame (Left)")
        self.next_frame_button = QPushButton(self)
        self.next_frame_button.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_MediaSeekForward))
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
        controls.setContentsMargins(8, 4, 8, 6)
        controls.addWidget(self.prev_frame_button)
        controls.addWidget(self.play_button)
        controls.addWidget(self.next_frame_button)
        controls.addWidget(self.slider, 1)
        controls.addWidget(self.time_label)
        controls.addSpacing(8)
        controls.addWidget(QLabel("Vol", self))
        controls.addWidget(self.volume)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.canvas, 1)
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
        self.volume.valueChanged.connect(lambda v: self.audio.setVolume(v / 100))

        self._set_enabled(False)

    # ----------------------------------------------------------------- API
    @property
    def media(self) -> MediaInfo | None:
        return self._media

    def load(self, media: MediaInfo) -> None:
        self._media = media
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
        self._set_enabled(False)

    def toggle_play(self) -> None:
        if self._media is None:
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            if self._duration_ms and self.player.position() >= self._duration_ms - self._frame_ms:
                self.player.setPosition(0)
            self.player.play()

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
            self.canvas.set_image(frame.toImage())

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
        icon = (
            QStyle.StandardPixmap.SP_MediaPause
            if state == QMediaPlayer.PlaybackState.PlayingState
            else QStyle.StandardPixmap.SP_MediaPlay
        )
        self.play_button.setIcon(self.style().standardIcon(icon))

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
