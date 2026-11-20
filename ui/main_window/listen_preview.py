"""Timeline → Listen: hear the voice cleanup and loudness before exporting.

FFmpeg renders a few seconds from the playhead to a WAV in the background;
a separate player plays it (the main preview player is paused meanwhile)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QUrl
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

from core.audio.enhance import render_preview
from core.render.settings import RenderSettings
from core.workers import Worker

if TYPE_CHECKING:
    from ui.main_window.main_window import MainWindow

log = logging.getLogger(__name__)

LISTEN_SECONDS = 8.0


class ListenPreview(QObject):
    def __init__(self, window: "MainWindow") -> None:
        super().__init__(window)
        self.window = window
        self.button = window.timeline_panel.listen_button
        self.player: QMediaPlayer | None = None  # created on first use
        self._workers: set[Worker] = set()
        self._request = 0
        window.timeline_panel.listen_requested.connect(self.toggle)

    def loudness_target(self) -> float:
        settings = self.window.config.export_settings
        return RenderSettings.from_dict(settings).loudness_lufs if settings else RenderSettings().loudness_lufs

    def toggle(self, on: bool) -> None:
        if on:
            self.start()
        else:
            self.stop()

    def start(self) -> bool:
        w = self.window
        media, timeline = w.preview.media, w.timeline.timeline
        if media is None or timeline is None or w.ffmpeg is None or not media.has_audio:
            self._reset_button()
            return False
        if w.preview.is_playing:
            w.preview.toggle_play()
        self._request += 1
        request = self._request
        output = w.config.cache_dir / "listen" / f"listen-{request % 2}.wav"
        start_s = w.timeline.playhead_us / 1_000_000
        worker = Worker(render_preview, w.ffmpeg.ffmpeg, Path(media.path), start_s, LISTEN_SECONDS, output,
                        timeline.denoise, timeline.gain_db, self.loudness_target())
        worker.signals.finished.connect(lambda path, wk=worker, r=request: self._ready(wk, r, path))
        worker.signals.failed.connect(lambda exc, wk=worker: self._failed(wk, exc))
        self._workers.add(worker)
        w.statusBar().showMessage("Preparing the sound…", 3000)
        w.pool.start(worker)
        return True

    def _ready(self, worker: Worker, request: int, path: Path) -> None:
        self._workers.discard(worker)
        if request != self._request or not self.button.isChecked():
            return
        if self.player is None:
            self.player = QMediaPlayer(self)
            self.player.setAudioOutput(QAudioOutput(self.player))
            self.player.mediaStatusChanged.connect(self._status)
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.player.play()
        self.window.statusBar().showMessage("Playing with noise reduction and loudness…", int(LISTEN_SECONDS * 1000))

    def _failed(self, worker: Worker, exc) -> None:
        self._workers.discard(worker)
        log.warning("Listen preview failed: %s", exc)
        self.window.statusBar().showMessage(f"Could not prepare the sound: {exc}", 6000)
        self._reset_button()

    def _status(self, status) -> None:
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self._reset_button()

    def stop(self) -> None:
        self._request += 1
        if self.player is not None and self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()  # stop() can block in Qt's FFmpeg backend without an audio device

    def _reset_button(self) -> None:
        self.button.blockSignals(True)
        self.button.setChecked(False)
        self.button.blockSignals(False)
        self.stop()
