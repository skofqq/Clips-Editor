"""Timeline widget: ruler, video segments, audio waveform, mute regions.

The horizontal axis is **source time**. Kept segments are highlighted and
cut-away parts are dimmed, so the waveform, the mute regions and (later)
the transcript always line up with the original recording.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from PySide6.QtCore import QLineF, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QLinearGradient, QMouseEvent, QPainter, QPen, QPolygonF, QWheelEvent
from PySide6.QtWidgets import QSizePolicy, QWidget

from core.audio.waveform import Waveform
from core.censor.review import ACCEPTED, PENDING, CensorState
from core.timeline.model import BEEP, Timeline
from ui.theme import palette
from core.timeline.timebase import US, format_timecode, frame_to_us, us_to_frame
from core.transcription.model import Transcript
from ui.timeline.timeline_controller import MUTE, SEGMENT, TimelineController

RULER_H = 22
TRACK_GAP = 4
# Default track layout (the video, audio and speech tracks can be resized).
VIDEO_Y, VIDEO_H = 26, 30
AUDIO_Y, AUDIO_H = 60, 72
MUTE_Y, MUTE_H = 136, 24
SPEECH_Y, SPEECH_H = 164, 22
CONTENT_H = SPEECH_Y + SPEECH_H + 6
HEIGHT_LIMITS = {"video": (20, 90), "audio": (36, 280), "speech": (22, 64)}
EDGE_GRAB = 6  # px within which an edge is grabbed
SNAP_PX = 8
DRAG_THRESHOLD = 3
MIN_PPS, MAX_PPS = 2.0, 4000.0  # pixels per second
MIN_WAVE_GAIN, MAX_WAVE_GAIN = 1.0, 12.0
FRAME_TICK_PX = 6  # frame ticks appear once a frame is this wide
_TICK_STEPS = (0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800, 3600)


@dataclass(frozen=True)
class TrackLayout:
    """Vertical layout of the tracks; every y is derived from the heights."""

    video_h: int = VIDEO_H
    audio_h: int = AUDIO_H
    speech_h: int = SPEECH_H

    @property
    def video_y(self) -> int:
        return RULER_H + TRACK_GAP

    @property
    def audio_y(self) -> int:
        return self.video_y + self.video_h + TRACK_GAP

    @property
    def mute_y(self) -> int:
        return self.audio_y + self.audio_h + TRACK_GAP

    @property
    def mute_h(self) -> int:
        return MUTE_H

    @property
    def speech_y(self) -> int:
        return self.mute_y + MUTE_H + TRACK_GAP

    @property
    def bottom(self) -> int:
        return self.speech_y + self.speech_h

    @property
    def content_h(self) -> int:
        return self.bottom + 6

    def rows(self) -> list[tuple[str, int, int]]:
        """(name, y, height) of each track, top to bottom."""
        return [
            ("video", self.video_y, self.video_h),
            ("audio", self.audio_y, self.audio_h),
            ("mute", self.mute_y, MUTE_H),
            ("speech", self.speech_y, self.speech_h),
        ]

    def handle_at(self, y: float) -> str | None:
        """Name of the resizable track whose lower edge is at ``y``."""
        for name, top, height in self.rows():
            if name in HEIGHT_LIMITS and top + height - 2 <= y <= top + height + TRACK_GAP + 1:
                return name
        return None

    def resized(self, name: str, height: int) -> "TrackLayout":
        low, high = HEIGHT_LIMITS[name]
        return replace(self, **{f"{name}_h": max(low, min(high, int(height)))})

    def to_dict(self) -> dict:
        return {"video": self.video_h, "audio": self.audio_h, "speech": self.speech_h}

    @classmethod
    def from_dict(cls, data: dict | None) -> "TrackLayout":
        layout = cls()
        for name in HEIGHT_LIMITS:
            try:
                layout = layout.resized(name, int((data or {})[name]))
            except (KeyError, TypeError, ValueError):
                pass
        return layout

_ACCENT = palette.accent()
C_BG = QColor(palette.BASE_ALT)
C_RULER = QColor(palette.BASE)
C_TRACK = QColor(palette.LAYER)
C_SEGMENT = QColor(palette.mix(palette.BASE, _ACCENT.fill, 0.42))
C_SEGMENT_SEL = QColor(palette.mix(palette.BASE, _ACCENT.fill, 0.72))
C_SEGMENT_EDGE = QColor(palette.mix(palette.BASE, _ACCENT.fill, 0.7))
C_WAVE = QColor(_ACCENT.text)
C_WAVE_DIM = QColor(palette.themed("#3a3a3a"))
C_MUTE = QColor("#ff6b7a")
C_BEEP = QColor("#ffb34d")
C_PLAYHEAD = QColor(palette.TEXT)
C_TEXT = QColor(palette.TEXT_TERTIARY)
C_SPEECH = QColor(palette.themed("#2f5a3f"))
C_SPEECH_TEXT = QColor(palette.themed("#dfffee"))
C_WORD_TEXT = QColor("#ffffff" if palette.is_light() else "#0d1f16")
C_STRONG = QColor(palette.TEXT)  # selection outlines
C_ON_SEGMENT = QColor(palette.TEXT if palette.is_light() else "#ffffff")
_LIGHT = palette.is_light()
C_SHADE = QColor(255, 255, 255, 150) if _LIGHT else QColor(0, 0, 0, 110)  # cut-away parts
C_FADE = QColor(255, 255, 255, 160) if _LIGHT else QColor(0, 0, 0, 120)
C_RANGE = QColor(0, 0, 0, 30) if _LIGHT else QColor(255, 255, 255, 45)
C_RANGE_EDGE = QColor(0, 0, 0, 140) if _LIGHT else QColor(255, 255, 255, 160)
C_WORD = QColor(palette.SUCCESS)
C_SUGGEST = QColor(palette.CAUTION)  # pending censor suggestion


class TimelineView(QWidget):
    seek_requested = Signal(int)  # microseconds
    view_changed = Signal()  # zoom or scroll changed
    gesture_finished = Signal()  # a drag ended (closes the undo step)
    word_clicked = Signal(int, int)  # start_us, end_us of an exactly timed word
    tracks_changed = Signal()  # a track height or the waveform gain changed

    def __init__(self, controller: TimelineController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.controller = controller
        self._waveform: Waveform | None = None
        self._transcript: Transcript | None = None
        self._censor: CensorState | None = None
        self._current_suggestion: int | None = None
        self._pps = 100.0
        self._scroll_us = 0
        self._fit_pending = True
        self._drag: dict | None = None
        self._wave_cache: tuple | None = None
        self._tracks = TrackLayout()
        self._wave_gain = 1.0
        self.setMinimumHeight(self._tracks.content_h)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        controller.changed.connect(self._on_model_changed)
        controller.selection_changed.connect(self.update)
        controller.playhead_changed.connect(self._on_playhead)

    # ------------------------------------------------------------- state
    @property
    def timeline(self) -> Timeline | None:
        return self.controller.timeline

    @property
    def pixels_per_second(self) -> float:
        return self._pps

    @property
    def scroll_us(self) -> int:
        return self._scroll_us

    def visible_us(self) -> int:
        return int(self.width() / self._pps * US)

    # ------------------------------------------------------ tracks / wave
    @property
    def tracks(self) -> TrackLayout:
        return self._tracks

    def set_tracks(self, tracks: TrackLayout) -> None:
        if tracks == self._tracks:
            return
        self._tracks = tracks
        self.setMinimumHeight(tracks.content_h)
        self.updateGeometry()
        self.tracks_changed.emit()
        self.update()

    def resize_track(self, name: str, height: int) -> None:
        self.set_tracks(self._tracks.resized(name, height))

    @property
    def wave_gain(self) -> float:
        return self._wave_gain

    def set_wave_gain(self, gain: float) -> None:
        """Vertical zoom of the waveform (display only, the sound is unchanged)."""
        gain = round(max(MIN_WAVE_GAIN, min(MAX_WAVE_GAIN, float(gain))), 2)
        if gain != self._wave_gain:
            self._wave_gain = gain
            self.tracks_changed.emit()
            self.update()

    def step_wave_gain(self, steps: float) -> None:
        self.set_wave_gain(self._wave_gain * (1.25 ** steps))

    def view_state(self) -> dict:
        return {"tracks": self._tracks.to_dict(), "wave_gain": self._wave_gain}

    def restore_view_state(self, data: dict | None) -> None:
        data = data or {}
        self.set_tracks(TrackLayout.from_dict(data.get("tracks")))
        try:
            self.set_wave_gain(float(data.get("wave_gain", 1.0)))
        except (TypeError, ValueError):
            pass

    def set_waveform(self, waveform: Waveform | None) -> None:
        self._waveform = waveform
        self._wave_cache = None
        self.update()

    def set_transcript(self, transcript: Transcript | None) -> None:
        self._transcript = transcript
        self.update()

    def set_censor(self, state: CensorState | None, current: int | None = None) -> None:
        """Show censor suggestions (outlines on the mute track); nothing is muted by this."""
        self._censor = state
        self._current_suggestion = current
        self.update()

    def speech_item_at(self, x: float):
        """(start_us, end_us, exact) of the word, else segment, under ``x``."""
        t = self._transcript
        if t is None:
            return None
        us = self.x_to_us(x)
        segment = t.segment_at(us)
        if segment is None:
            return None
        word = next((w for w in segment.words if w.exact and w.start_us <= us < w.end_us), None)
        if word is not None:
            return word.start_us, word.end_us, True
        return segment.start_us, segment.end_us, False

    def request_fit(self) -> None:
        """Fit the whole clip on the next layout (used when a clip is loaded)."""
        self.zoom_fit()

    def us_to_x(self, us: int) -> float:
        return (us - self._scroll_us) * self._pps / US

    def x_to_us(self, x: float) -> int:
        t = self.timeline
        us = int(round(self._scroll_us + x * US / self._pps))
        return max(0, min(us, t.duration_us)) if t else max(0, us)

    # -------------------------------------------------------- zoom/scroll
    def set_scroll_us(self, us: int) -> None:
        t = self.timeline
        limit = max(0, (t.duration_us if t else 0) - self.visible_us() + int(20 * US / self._pps))
        us = max(0, min(int(us), limit))
        if us != self._scroll_us:
            self._scroll_us = us
            self.view_changed.emit()
            self.update()

    def set_zoom(self, pps: float, anchor_x: float | None = None) -> None:
        pps = max(MIN_PPS, min(MAX_PPS, pps))
        if abs(pps - self._pps) < 1e-9:
            return
        anchor_x = self.width() / 2 if anchor_x is None else anchor_x
        anchor_us = self._scroll_us + anchor_x * US / self._pps
        self._pps = pps
        self._fit_pending = False
        self._scroll_us = -1  # force set_scroll_us to apply clamping and emit
        self.set_scroll_us(int(anchor_us - anchor_x * US / pps))
        self.view_changed.emit()
        self.update()

    def _zoom_anchor(self) -> float | None:
        """Zoom around the mouse when it is over the timeline, else the playhead."""
        pos = self.mapFromGlobal(self.cursor().pos())
        if self.underMouse() and 0 <= pos.x() <= self.width():
            return float(pos.x())
        if self._playhead_visible():
            return self.us_to_x(self.controller.playhead_us)
        return None

    def zoom_in(self) -> None:
        self.set_zoom(self._pps * 1.5, self._zoom_anchor())

    def zoom_out(self) -> None:
        self.set_zoom(self._pps / 1.5, self._zoom_anchor())

    def zoom_to_range(self, start_us: int, end_us: int) -> None:
        """Fill the view with ``start_us``–``end_us`` (with a small margin)."""
        start_us, end_us = sorted((int(start_us), int(end_us)))
        length = max(1, end_us - start_us)
        margin_px = 24
        pps = (self.width() - 2 * margin_px) / (length / US)
        self.set_zoom(max(MIN_PPS, min(MAX_PPS, pps)))
        center = (start_us + end_us) / 2
        self.set_scroll_us(int(center - self.width() / 2 * US / self._pps))

    def selection_bounds(self) -> tuple[int, int] | None:
        """The selected range, else the selected mute region or segment."""
        c, t = self.controller, self.timeline
        if t is None:
            return None
        if c.range:
            return c.range
        if c.selected and c.selected[0] == MUTE:
            region = t.mute(c.selected[1])
            return (region.start_us, region.end_us) if region else None
        if c.selected and c.selected[0] == SEGMENT and 0 <= c.selected[1] < len(t.segments):
            seg = t.segments[c.selected[1]]
            return seg.start_us, seg.end_us
        return None

    def zoom_selection(self) -> bool:
        bounds = self.selection_bounds()
        if bounds is None:
            return False
        self.zoom_to_range(*bounds)
        return True

    def frame_px(self) -> float:
        """Width of one video frame at the current zoom (0 when fps is unknown)."""
        fps = self.controller.fps
        return self._pps / float(fps) if fps else 0.0

    def zoom_fit(self) -> None:
        t = self.timeline
        self._fit_pending = True  # stay fitted while the panel is resized
        if t is None or self.width() < 40:
            return
        self._pps = max(MIN_PPS, min(MAX_PPS, (self.width() - 20) / (t.duration_us / US)))
        self._scroll_us = 0
        self.view_changed.emit()
        self.update()

    def _playhead_visible(self) -> bool:
        return 0 <= self.us_to_x(self.controller.playhead_us) <= self.width()

    def resizeEvent(self, event) -> None:  # noqa: N802
        if self._fit_pending:
            self.zoom_fit()
        self.view_changed.emit()

    def _on_model_changed(self) -> None:
        self.update()

    def _on_playhead(self, us: int) -> None:
        # Keep the playhead in view while playing.
        x = self.us_to_x(us)
        if self._drag is None and (x < 0 or x > self.width()):
            self.set_scroll_us(us - int(self.width() * 0.1 * US / self._pps))
        self.update()

    # ------------------------------------------------------------ hit test
    def _snap(self, us: int, exclude: set[int] = frozenset(), enabled: bool = True) -> int:
        t = self.timeline
        if t is None or not enabled:
            return us
        targets = {0, t.duration_us, self.controller.playhead_us}
        for s in t.segments:
            targets.update((s.start_us, s.end_us))
        for m in t.mutes:
            targets.update((m.start_us, m.end_us))
        if self.controller.range:
            targets.update(self.controller.range)
        if self._transcript is not None:
            for word in self._transcript.words():
                if word.exact:
                    targets.update((word.start_us, word.end_us))
        targets -= set(exclude)
        if not targets:
            return us
        best = min(targets, key=lambda v: abs(v - us))
        return best if abs(best - us) * self._pps / US <= SNAP_PX else us

    def hit_test(self, pos: QPointF) -> dict:
        """Describe what is under ``pos``: {'area': ..., ...}."""
        t = self.timeline
        x, y = pos.x(), pos.y()
        if t is None:
            return {"area": "none"}
        tr = self._tracks
        if y < RULER_H:
            return {"area": "ruler"}
        handle = tr.handle_at(y)
        if handle is not None:
            return {"area": "track_resize", "track": handle}
        if tr.speech_y <= y <= tr.bottom:
            item = self.speech_item_at(x)
            return {"area": "speech", "item": item} if item else {"area": "speech_track"}
        if tr.mute_y <= y <= tr.mute_y + MUTE_H:
            # Edges first, so that a short region can still be resized.
            for m in t.mutes:
                x0, x1 = self.us_to_x(m.start_us), self.us_to_x(m.end_us)
                grab = min(EDGE_GRAB, max(2.0, (x1 - x0) / 3))
                if abs(x - x0) <= grab:
                    return {"area": "mute_edge", "id": m.id, "edge": "start"}
                if abs(x - x1) <= grab:
                    return {"area": "mute_edge", "id": m.id, "edge": "end"}
            for m in t.mutes:
                if self.us_to_x(m.start_us) <= x <= self.us_to_x(m.end_us):
                    return {"area": "mute", "id": m.id}
            return {"area": "mute_track"}
        if tr.video_y <= y <= tr.audio_y + tr.audio_h:
            for index, s in enumerate(t.segments):
                x0, x1 = self.us_to_x(s.start_us), self.us_to_x(s.end_us)
                # At a split point two edges coincide: the side of the
                # cursor decides which segment's edge is grabbed.
                if abs(x - x1) <= EDGE_GRAB and x <= x1 + 0.5:
                    return {"area": "segment_edge", "index": index, "edge": "end"}
                if abs(x - x0) <= EDGE_GRAB and x >= x0 - 0.5:
                    return {"area": "segment_edge", "index": index, "edge": "start"}
            for index, s in enumerate(t.segments):
                x0, x1 = self.us_to_x(s.start_us), self.us_to_x(s.end_us)
                if abs(x - x0) <= EDGE_GRAB:
                    return {"area": "segment_edge", "index": index, "edge": "start"}
                if abs(x - x1) <= EDGE_GRAB:
                    return {"area": "segment_edge", "index": index, "edge": "end"}
            index = t.segment_index_at(self.x_to_us(x))
            return {"area": "segment", "index": index} if index is not None else {"area": "gap"}
        return {"area": "none"}

    # -------------------------------------------------------------- mouse
    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        t = self.timeline
        if event.button() != Qt.MouseButton.LeftButton or t is None:
            return
        pos = event.position()
        hit = self.hit_test(pos)
        us = self.x_to_us(pos.x())
        area = hit["area"]
        self._drag = {"hit": hit, "start_x": pos.x(), "start_us": us, "moved": False}
        if area == "ruler":
            self.seek_requested.emit(self.controller.frame_snap(us))
        elif area == "track_resize":
            self._drag["start_y"] = pos.y()
            self._drag["start_h"] = getattr(self._tracks, f"{hit['track']}_h")
        elif area == "mute":
            self.controller.select(MUTE, hit["id"])
            self._drag["offset_us"] = us - t.mute(hit["id"]).start_us
        elif area == "mute_edge":
            self.controller.select(MUTE, hit["id"])
        elif area == "segment_edge":
            self.controller.select(SEGMENT, hit["index"])

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        pos = event.position()
        if self._drag is None:
            area = self.hit_test(pos)["area"]
            if area == "track_resize":
                self.setCursor(Qt.CursorShape.SizeVerCursor)
            elif area in ("mute_edge", "segment_edge"):
                self.setCursor(Qt.CursorShape.SizeHorCursor)
            elif area == "mute":
                self.setCursor(Qt.CursorShape.OpenHandCursor)
            else:
                self.setCursor(Qt.CursorShape.ArrowCursor)
            return
        t = self.timeline
        if t is None:
            return
        drag = self._drag
        if drag["hit"]["area"] == "track_resize":
            self.resize_track(drag["hit"]["track"], drag["start_h"] + pos.y() - drag["start_y"])
            drag["moved"] = True
            return
        if not drag["moved"] and abs(pos.x() - drag["start_x"]) < DRAG_THRESHOLD:
            return
        drag["moved"] = True
        hit = drag["hit"]
        area = hit["area"]
        snap = not (event.modifiers() & Qt.KeyboardModifier.AltModifier)
        us = self.x_to_us(pos.x())
        c = self.controller

        if area == "ruler":
            self.seek_requested.emit(c.frame_snap(us))
        elif area == "segment_edge":
            index, edge = hit["index"], hit["edge"]
            seg = t.segments[index]
            own = {seg.start_us, seg.end_us}
            us = c.frame_snap(self._snap(us, exclude=own, enabled=snap))
            kwargs = {"start_us": us} if edge == "start" else {"end_us": us}
            c.set_timeline(t.trim_segment(index, **kwargs), key=("trim", index, edge))
        elif area == "mute_edge":
            region = t.mute(hit["id"])
            if region is None:
                return
            us = self._snap(us, exclude={region.start_us, region.end_us}, enabled=snap)
            us = c.frame_snap(us) if snap else us
            kwargs = {"start_us": us} if hit["edge"] == "start" else {"end_us": us}
            c.set_timeline(t.update_mute(hit["id"], **kwargs), key=("mute_edge", hit["id"], hit["edge"]))
        elif area == "mute":
            region = t.mute(hit["id"])
            if region is None:
                return
            start = us - drag["offset_us"]
            own = {region.start_us, region.end_us}
            snapped_start = self._snap(start, exclude=own, enabled=snap)
            if snapped_start == start:  # try snapping the right edge instead
                snapped_end = self._snap(start + region.length_us, exclude=own, enabled=snap)
                snapped_start = snapped_end - region.length_us
            if snap:
                snapped_start = c.frame_snap(snapped_start)
            c.set_timeline(t.move_mute(hit["id"], snapped_start), key=("mute_move", hit["id"]))
        else:  # segment body, gap or mute track: select a time range
            start = drag["start_us"]
            end = self._snap(us, exclude={start}, enabled=snap)
            if snap:
                start, end = c.frame_snap(start), c.frame_snap(end)
            c.set_range(start, end)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        drag, self._drag = self._drag, None
        if drag is None or event.button() != Qt.MouseButton.LeftButton:
            return
        hit = drag["hit"]
        if hit["area"] == "track_resize":
            return
        if not drag["moved"]:
            # A plain click: move the playhead and select what was clicked.
            area = hit["area"]
            if area == "speech":
                start, end, exact = hit["item"]
                self.seek_requested.emit(start)
                if exact:  # select the word so it can be muted with M
                    self.controller.set_range(start, end)
                    self.word_clicked.emit(start, end)
                else:
                    self.controller.set_range(None)
            elif area in ("segment", "gap", "mute_track", "speech_track"):
                self.controller.set_range(None)
                self.seek_requested.emit(drag["start_us"])
                if area == "segment":
                    self.controller.select(SEGMENT, hit["index"])
                else:
                    self.controller.select(None)
        self.gesture_finished.emit()

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        delta = event.angleDelta()
        mods = event.modifiers()
        if mods & Qt.KeyboardModifier.ControlModifier:
            self.set_zoom(self._pps * (1.2 ** (delta.y() / 120)), event.position().x())
        elif mods & Qt.KeyboardModifier.AltModifier:
            # Alt+wheel: waveform height (Windows reports it as a horizontal wheel).
            step = delta.y() or delta.x()
            self.step_wave_gain(step / 120)
        else:
            step = delta.x() if delta.x() else delta.y()
            self.set_scroll_us(self._scroll_us - int(step / 120 * 80 * US / self._pps))
        event.accept()

    # -------------------------------------------------------------- paint
    def _wave_columns(self, width: int):
        key = (id(self._waveform), self._scroll_us, round(self._pps, 6), width)
        if self._wave_cache is None or self._wave_cache[0] != key:
            start_s = self._scroll_us / US
            cols = self._waveform.columns(start_s, start_s + width / self._pps, width)
            self._wave_cache = (key, cols)
        return self._wave_cache[1]

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), C_BG)
        t = self.timeline
        if t is None:
            p.setPen(QColor(palette.TEXT_TERTIARY))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Import a video to edit it on the timeline")
            return
        c = self.controller
        x_end = self.us_to_x(t.duration_us)
        tr = self._tracks
        video_y, video_h, audio_y, audio_h = tr.video_y, tr.video_h, tr.audio_y, tr.audio_h
        mute_y, mute_h, speech_y, speech_h = tr.mute_y, MUTE_H, tr.speech_y, tr.speech_h

        # Ruler band and rounded track backgrounds.
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.fillRect(QRectF(0, 0, w, RULER_H), C_RULER)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(C_TRACK)
        track_w = min(w + 8.0, max(0.0, x_end))
        for y, height in ((video_y, video_h), (audio_y, audio_h), (mute_y, mute_h)):
            p.drawRoundedRect(QRectF(0, y, track_w, height), 6, 6)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        # Frame ticks once a single frame is wide enough to aim at.
        frame_px = self.frame_px()
        if frame_px >= FRAME_TICK_PX:
            fps = c.fps
            first_frame = us_to_frame(self._scroll_us, fps)
            last_frame = us_to_frame(min(t.duration_us, self.x_to_us(w)), fps) + 1
            p.setPen(QColor(palette.TEXT_TERTIARY))
            for frame in range(first_frame, last_frame + 1):
                fx = self.us_to_x(frame_to_us(frame, fps))
                p.drawLine(QLineF(fx, RULER_H - 4, fx, RULER_H))

        # Ruler.
        seconds_per_tick = next((s for s in _TICK_STEPS if s * self._pps >= 70), _TICK_STEPS[-1])
        first = int(self._scroll_us / US / seconds_per_tick)
        p.setPen(C_TEXT)
        tick = first
        while True:
            seconds = tick * seconds_per_tick
            x = self.us_to_x(int(seconds * US))
            if x > w:
                break
            if x >= -60 and seconds * US <= t.duration_us:
                p.drawLine(QLineF(x, RULER_H - 7, x, RULER_H))
                label = format_timecode(int(round(seconds * US)), show_ms=seconds_per_tick < 1)
                p.drawText(QPointF(x + 3, RULER_H - 8), label)
            tick += 1
            if seconds * US > t.duration_us:
                break

        # Video segments.
        for index, s in enumerate(t.segments):
            x0, x1 = self.us_to_x(s.start_us), self.us_to_x(s.end_us)
            if x1 < 0 or x0 > w:
                continue
            rect = QRectF(x0, video_y, x1 - x0, video_h)
            selected = c.selected == (SEGMENT, index)
            gradient = QLinearGradient(rect.topLeft(), rect.bottomLeft())
            base = C_SEGMENT_SEL if selected else C_SEGMENT
            gradient.setColorAt(0, base.lighter(118))
            gradient.setColorAt(1, base)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            p.setBrush(gradient)
            p.setPen(QPen(C_STRONG if selected else C_SEGMENT_EDGE, 1))
            p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 6, 6)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            if rect.width() > 60:
                p.setPen(C_ON_SEGMENT)
                p.drawText(
                    rect.adjusted(6, 0, -4, 0),
                    Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                    format_timecode(s.length_us),
                )

        # Waveform: bright inside kept segments, dim where cut.
        mid = audio_y + audio_h / 2
        if self._waveform is not None and len(self._waveform.peaks):
            cols = self._wave_columns(w)
            half = audio_h / 2 - 2
            kept: list[QLineF] = []
            cut: list[QLineF] = []
            bounds = [(self.us_to_x(s.start_us), self.us_to_x(s.end_us)) for s in t.segments]
            bi = 0
            for x in range(w):
                amp = min(1.0, float(cols[x]) * self._wave_gain) * half
                if amp < 0.5:
                    amp = 0.5
                while bi < len(bounds) and bounds[bi][1] <= x:
                    bi += 1
                inside = bi < len(bounds) and bounds[bi][0] <= x
                (kept if inside else cut).append(QLineF(x + 0.5, mid - amp, x + 0.5, mid + amp))
            p.setPen(QPen(C_WAVE_DIM, 1))
            p.drawLines(cut)
            p.setPen(QPen(C_WAVE, 1))
            p.drawLines(kept)
        else:
            p.setPen(QColor(palette.TEXT_TERTIARY))
            p.drawText(QRectF(8, audio_y, w, audio_h), Qt.AlignmentFlag.AlignVCenter, "No waveform")

        # Fades (drawn over the audio track in output order).
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(C_FADE)
        if t.fade_in_us > 0:
            x0 = self.us_to_x(t.start_us)
            x1 = self.us_to_x(t.output_to_source(t.fade_in_us))
            p.drawPolygon(QPolygonF([QPointF(x0, audio_y), QPointF(x1, audio_y), QPointF(x0, audio_y + audio_h)]))
        if t.fade_out_us > 0:
            x1 = self.us_to_x(t.end_us)
            x0 = self.us_to_x(t.output_to_source(t.output_us - t.fade_out_us))
            p.drawPolygon(QPolygonF([QPointF(x0, audio_y), QPointF(x1, audio_y), QPointF(x1, audio_y + audio_h)]))

        # Mute regions: tint on the audio track, blocks on the mute track.
        for m in t.mutes:
            x0, x1 = self.us_to_x(m.start_us), self.us_to_x(m.end_us)
            if x1 < 0 or x0 > w:
                continue
            color = C_BEEP if m.mode == BEEP else C_MUTE
            tint = QColor(color)
            tint.setAlpha(70)
            p.fillRect(QRectF(x0, audio_y, x1 - x0, audio_h), tint)
            block = QRectF(x0, mute_y, max(2.0, x1 - x0), mute_h)
            p.fillRect(block, color)
            if c.selected == (MUTE, m.id):
                p.setPen(QPen(C_STRONG, 2))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRect(block.adjusted(1, 1, -1, -1))
            if block.width() > 34:
                p.setPen(QColor("#101014"))
                p.drawText(block, Qt.AlignmentFlag.AlignCenter, "BEEP" if m.mode == BEEP else "MUTE")

        # Censor suggestions: dashed = waiting for review, solid = accepted.
        if self._censor is not None:
            for sug in self._censor.suggestions:
                if sug.status not in (PENDING, ACCEPTED):
                    continue
                x0, x1 = self.us_to_x(sug.start_us), self.us_to_x(sug.end_us)
                if x1 < 0 or x0 > w:
                    continue
                pen = QPen(C_SUGGEST if sug.status == PENDING else C_STRONG, 2)
                if sug.status == PENDING:
                    pen.setStyle(Qt.PenStyle.DashLine)
                if sug.id == self._current_suggestion:
                    pen.setWidth(3)
                p.setPen(pen)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRect(QRectF(x0, mute_y + 1, max(3.0, x1 - x0), mute_h - 2))

        # Speech track: segments, and words when zoomed in far enough.
        p.fillRect(QRectF(0, speech_y, min(w, max(0.0, x_end)), speech_h), C_TRACK)
        if self._transcript is not None:
            show_words = self._pps >= 40
            metrics = p.fontMetrics()
            for seg in self._transcript.segments:
                x0, x1 = self.us_to_x(seg.start_us), self.us_to_x(seg.end_us)
                if x1 < 0 or x0 > w:
                    continue
                block = QRectF(x0, speech_y, max(2.0, x1 - x0), speech_h)
                p.fillRect(block, C_SPEECH)
                exact_words = [wd for wd in seg.words if wd.exact]
                if show_words and exact_words:
                    for wd in exact_words:
                        wx0, wx1 = self.us_to_x(wd.start_us), self.us_to_x(wd.end_us)
                        rect = QRectF(wx0, speech_y + 3, max(1.0, wx1 - wx0 - 1), speech_h - 6)
                        p.fillRect(rect, C_WORD)
                        if rect.width() > metrics.horizontalAdvance(wd.text) + 4:
                            p.setPen(C_WORD_TEXT)
                            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, wd.text)
                else:
                    if not seg.has_word_timing:
                        p.setPen(QPen(C_WORD, 1, Qt.PenStyle.DashLine))
                        p.setBrush(Qt.BrushStyle.NoBrush)
                        p.drawRect(block.adjusted(0.5, 0.5, -0.5, -0.5))
                    p.setPen(C_SPEECH_TEXT)
                    text = metrics.elidedText(seg.text, Qt.TextElideMode.ElideRight, int(block.width()) - 6)
                    p.drawText(block.adjusted(3, 0, -3, 0), Qt.AlignmentFlag.AlignVCenter, text)

        # Dim everything that is cut away.
        p.setPen(Qt.PenStyle.NoPen)
        dim = C_SHADE
        cursor = 0.0
        for s in t.segments:
            x0 = self.us_to_x(s.start_us)
            if x0 > cursor:
                p.fillRect(QRectF(cursor, video_y, x0 - cursor, speech_y + speech_h - video_y), dim)
            cursor = max(cursor, self.us_to_x(s.end_us))
        if x_end > cursor:
            p.fillRect(QRectF(cursor, video_y, x_end - cursor, speech_y + speech_h - video_y), dim)

        # Range selection.
        if c.range:
            x0, x1 = self.us_to_x(c.range[0]), self.us_to_x(c.range[1])
            bottom = speech_y + speech_h
            p.fillRect(QRectF(x0, RULER_H, x1 - x0, bottom - RULER_H), C_RANGE)
            p.setPen(QPen(C_RANGE_EDGE, 1))
            p.drawLine(QLineF(x0, RULER_H, x0, bottom))
            p.drawLine(QLineF(x1, RULER_H, x1, bottom))

        # Playhead.
        x = self.us_to_x(c.playhead_us)
        if -2 <= x <= w + 2:
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            p.setPen(QPen(C_PLAYHEAD, 1.5))
            p.drawLine(QLineF(x, 6, x, h))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(C_PLAYHEAD)
            p.drawRoundedRect(QRectF(x - 5, 1, 10, 9), 3, 3)
            p.drawPolygon(QPolygonF([QPointF(x - 5, 8), QPointF(x + 5, 8), QPointF(x, 13)]))
