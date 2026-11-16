"""Makes preview playback follow the timeline edit.

While playing, cut-away parts are skipped, playback stops at the end of
the edit, and the preview volume follows mute regions, gain and fades.
Mute regions set to Beep are previewed as silence; the tone is generated
at render time.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtMultimedia import QMediaPlayer

from core.timeline.timebase import ms_to_us
from ui.preview.preview_panel import PreviewPanel
from ui.timeline.timeline_controller import TimelineController

TICK_MS = 15
JUMP_TOLERANCE_US = 30_000  # do not re-seek for gaps shorter than this


def us_to_ms_ceil(us: int) -> int:
    return -(-int(us) // 1000)


class EditPlayback(QObject):
    def __init__(self, preview: PreviewPanel, controller: TimelineController, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.preview = preview
        self.controller = controller
        self.player = preview.player
        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self.tick)
        self._active = True
        # Queued: never call back into the player from inside its own signal
        # (changing volume or position re-entrantly can deadlock the backend).
        queued = Qt.ConnectionType.QueuedConnection
        self.player.playbackStateChanged.connect(self._on_state, queued)
        self.player.positionChanged.connect(self._on_position, queued)
        controller.changed.connect(self.tick)

    def shutdown(self) -> None:
        """Stop reacting to the player (call before closing the window)."""
        self._active = False
        self._timer.stop()

    def _on_position(self, _position: int) -> None:
        self.tick()

    def _playing(self) -> bool:
        return self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    def _on_state(self, state: QMediaPlayer.PlaybackState) -> None:
        if not self._active:
            return
        state = self.player.playbackState()  # the signal is queued; use the live value
        if state == QMediaPlayer.PlaybackState.PlayingState:
            t = self.controller.timeline
            # Pressing play at (or after) the end of the edit starts over.
            if t is not None and t.next_kept(ms_to_us(self.player.position())) is None:
                self.player.setPosition(us_to_ms_ceil(t.start_us))
            self._timer.start()
        else:
            self._timer.stop()
        self.tick()

    def tick(self) -> None:
        if not self._active:
            return
        t = self.controller.timeline
        if t is None:
            self.preview.set_audio_factor(1.0)
            return
        pos_us = ms_to_us(self.player.position())
        if self._playing():
            target = t.next_kept(pos_us)
            if target is None:
                self.player.pause()
                self.player.setPosition(max(0, t.end_us // 1000 - 1))
                return
            if target - pos_us > JUMP_TOLERANCE_US:
                self.preview.set_audio_factor(0.0)
                self.player.setPosition(us_to_ms_ceil(target))
                return
            pos_us = max(pos_us, target)
        self.preview.set_audio_factor(min(1.0, t.volume_at(pos_us)))
