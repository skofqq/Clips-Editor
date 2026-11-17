"""Timeline widget: ruler, video segments, audio waveform, mute regions.

The horizontal axis is **source time**. Kept segments are highlighted and
cut-away parts are dimmed, so the waveform, the mute regions and (later)
the transcript always line up with the original recording.
"""

from __future__ import annotations

from PySide6.QtCore import QLineF, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPen, QPolygonF, QWheelEvent
from PySide6.QtWidgets import QSizePolicy, QWidget

from core.audio.waveform import Waveform
from core.censor.review import ACCEPTED, PENDING, CensorState
from core.timeline.model import BEEP, Timeline
from core.timeline.timebase import US, format_timecode
from core.transcription.model import Transcript
from ui.timeline.timeline_controller import MUTE, SEGMENT, TimelineController

RULER_H = 22
VIDEO_Y, VIDEO_H = 26, 30
AUDIO_Y, AUDIO_H = 60, 72
MUTE_Y, MUTE_H = 136, 24
SPEECH_Y, SPEECH_H = 164, 22
CONTENT_H = SPEECH_Y + SPEECH_H + 6
EDGE_GRAB = 6  # px within which an edge is grabbed
SNAP_PX = 8
DRAG_THRESHOLD = 3
MIN_PPS, MAX_PPS = 2.0, 2000.0  # pixels per second
_TICK_STEPS = (0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800, 3600)

C_BG = QColor("#18191c")
C_TRACK = QColor("#202124")
C_SEGMENT = QColor("#4a2fa8")
C_SEGMENT_SEL = QColor("#7c4dff")
C_WAVE = QColor("#35d0ff")
C_WAVE_DIM = QColor("#3a4a52")
C_MUTE = QColor("#ff4d6d")
C_BEEP = QColor("#ffa23a")
C_PLAYHEAD = QColor("#ffffff")
C_TEXT = QColor("#a9abb0")
C_SPEECH = QColor("#2e6b4f")
C_WORD = QColor("#3fbf7f")
C_SUGGEST = QColor("#ffd23a")  # pending censor suggestion


class TimelineView(QWidget):
    seek_requested = Signal(int)  # microseconds
    view_changed = Signal()  # zoom or scroll changed
    gesture_finished = Signal()  # a drag ended (closes the undo step)
    word_clicked = Signal(int, int)  # start_us, end_us of an exactly timed word

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
        self.setMinimumHeight(CONTENT_H)
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

    def zoom_in(self) -> None:
        self.set_zoom(self._pps * 1.5, self.us_to_x(self.controller.playhead_us) if self._playhead_visible() else None)

    def zoom_out(self) -> None:
        self.set_zoom(self._pps / 1.5)

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
        if y < RULER_H:
            return {"area": "ruler"}
        if SPEECH_Y <= y <= SPEECH_Y + SPEECH_H:
            item = self.speech_item_at(x)
            return {"area": "speech", "item": item} if item else {"area": "speech_track"}
        if MUTE_Y <= y <= MUTE_Y + MUTE_H:
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
        if VIDEO_Y <= y <= AUDIO_Y + AUDIO_H:
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
            self.seek_requested.emit(us)
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
            if area in ("mute_edge", "segment_edge"):
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
        if not drag["moved"] and abs(pos.x() - drag["start_x"]) < DRAG_THRESHOLD:
            return
        drag["moved"] = True
        hit = drag["hit"]
        area = hit["area"]
        snap = not (event.modifiers() & Qt.KeyboardModifier.AltModifier)
        us = self.x_to_us(pos.x())
        c = self.controller

        if area == "ruler":
            self.seek_requested.emit(us)
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
            c.set_timeline(t.move_mute(hit["id"], snapped_start), key=("mute_move", hit["id"]))
        else:  # segment body, gap or mute track: select a time range
            start = drag["start_us"]
            c.set_range(start, self._snap(us, exclude={start}, enabled=snap))

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        drag, self._drag = self._drag, None
        if drag is None or event.button() != Qt.MouseButton.LeftButton:
            return
        hit = drag["hit"]
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
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.set_zoom(self._pps * (1.2 ** (delta.y() / 120)), event.position().x())
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
            p.setPen(QColor("#6b6d72"))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Import a video to edit it on the timeline")
            return
        c = self.controller
        x_end = self.us_to_x(t.duration_us)

        # Track backgrounds.
        for y, height in ((VIDEO_Y, VIDEO_H), (AUDIO_Y, AUDIO_H), (MUTE_Y, MUTE_H)):
            p.fillRect(QRectF(0, y, min(w, max(0.0, x_end)), height), C_TRACK)

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
            rect = QRectF(x0, VIDEO_Y, x1 - x0, VIDEO_H)
            selected = c.selected == (SEGMENT, index)
            p.fillRect(rect, C_SEGMENT_SEL if selected else C_SEGMENT)
            p.setPen(QPen(QColor("#b9a2ff") if selected else QColor("#7c4dff"), 1))
            p.drawRect(rect.adjusted(0, 0, -1, -1))
            if rect.width() > 60:
                p.setPen(QColor("#ffffff"))
                p.drawText(
                    rect.adjusted(6, 0, -4, 0),
                    Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                    format_timecode(s.length_us),
                )

        # Waveform: bright inside kept segments, dim where cut.
        mid = AUDIO_Y + AUDIO_H / 2
        if self._waveform is not None and len(self._waveform.peaks):
            cols = self._wave_columns(w)
            half = AUDIO_H / 2 - 2
            kept: list[QLineF] = []
            cut: list[QLineF] = []
            bounds = [(self.us_to_x(s.start_us), self.us_to_x(s.end_us)) for s in t.segments]
            bi = 0
            for x in range(w):
                amp = float(cols[x]) * half
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
            p.setPen(QColor("#6b6d72"))
            p.drawText(QRectF(8, AUDIO_Y, w, AUDIO_H), Qt.AlignmentFlag.AlignVCenter, "No waveform")

        # Fades (drawn over the audio track in output order).
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 120))
        if t.fade_in_us > 0:
            x0 = self.us_to_x(t.start_us)
            x1 = self.us_to_x(t.output_to_source(t.fade_in_us))
            p.drawPolygon(QPolygonF([QPointF(x0, AUDIO_Y), QPointF(x1, AUDIO_Y), QPointF(x0, AUDIO_Y + AUDIO_H)]))
        if t.fade_out_us > 0:
            x1 = self.us_to_x(t.end_us)
            x0 = self.us_to_x(t.output_to_source(t.output_us - t.fade_out_us))
            p.drawPolygon(QPolygonF([QPointF(x0, AUDIO_Y), QPointF(x1, AUDIO_Y), QPointF(x1, AUDIO_Y + AUDIO_H)]))

        # Mute regions: tint on the audio track, blocks on the mute track.
        for m in t.mutes:
            x0, x1 = self.us_to_x(m.start_us), self.us_to_x(m.end_us)
            if x1 < 0 or x0 > w:
                continue
            color = C_BEEP if m.mode == BEEP else C_MUTE
            tint = QColor(color)
            tint.setAlpha(70)
            p.fillRect(QRectF(x0, AUDIO_Y, x1 - x0, AUDIO_H), tint)
            block = QRectF(x0, MUTE_Y, max(2.0, x1 - x0), MUTE_H)
            p.fillRect(block, color)
            if c.selected == (MUTE, m.id):
                p.setPen(QPen(QColor("#ffffff"), 2))
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
                pen = QPen(C_SUGGEST if sug.status == PENDING else QColor("#ffffff"), 2)
                if sug.status == PENDING:
                    pen.setStyle(Qt.PenStyle.DashLine)
                if sug.id == self._current_suggestion:
                    pen.setWidth(3)
                p.setPen(pen)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRect(QRectF(x0, MUTE_Y + 1, max(3.0, x1 - x0), MUTE_H - 2))

        # Speech track: segments, and words when zoomed in far enough.
        p.fillRect(QRectF(0, SPEECH_Y, min(w, max(0.0, x_end)), SPEECH_H), C_TRACK)
        if self._transcript is not None:
            show_words = self._pps >= 40
            metrics = p.fontMetrics()
            for seg in self._transcript.segments:
                x0, x1 = self.us_to_x(seg.start_us), self.us_to_x(seg.end_us)
                if x1 < 0 or x0 > w:
                    continue
                block = QRectF(x0, SPEECH_Y, max(2.0, x1 - x0), SPEECH_H)
                p.fillRect(block, C_SPEECH)
                exact_words = [wd for wd in seg.words if wd.exact]
                if show_words and exact_words:
                    for wd in exact_words:
                        wx0, wx1 = self.us_to_x(wd.start_us), self.us_to_x(wd.end_us)
                        rect = QRectF(wx0, SPEECH_Y + 3, max(1.0, wx1 - wx0 - 1), SPEECH_H - 6)
                        p.fillRect(rect, C_WORD)
                        if rect.width() > metrics.horizontalAdvance(wd.text) + 4:
                            p.setPen(QColor("#0d1f16"))
                            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, wd.text)
                else:
                    if not seg.has_word_timing:
                        p.setPen(QPen(C_WORD, 1, Qt.PenStyle.DashLine))
                        p.setBrush(Qt.BrushStyle.NoBrush)
                        p.drawRect(block.adjusted(0.5, 0.5, -0.5, -0.5))
                    p.setPen(QColor("#dfffee"))
                    text = metrics.elidedText(seg.text, Qt.TextElideMode.ElideRight, int(block.width()) - 6)
                    p.drawText(block.adjusted(3, 0, -3, 0), Qt.AlignmentFlag.AlignVCenter, text)

        # Dim everything that is cut away.
        p.setPen(Qt.PenStyle.NoPen)
        dim = QColor(0, 0, 0, 110)
        cursor = 0.0
        for s in t.segments:
            x0 = self.us_to_x(s.start_us)
            if x0 > cursor:
                p.fillRect(QRectF(cursor, VIDEO_Y, x0 - cursor, SPEECH_Y + SPEECH_H - VIDEO_Y), dim)
            cursor = max(cursor, self.us_to_x(s.end_us))
        if x_end > cursor:
            p.fillRect(QRectF(cursor, VIDEO_Y, x_end - cursor, SPEECH_Y + SPEECH_H - VIDEO_Y), dim)

        # Track captions.
        p.setPen(QColor(255, 255, 255, 90))
        p.drawText(QPointF(6, AUDIO_Y + 13), "Audio")
        p.drawText(QPointF(6, MUTE_Y + 16), "Mute" if not t.mutes else "")
        if self._transcript is None:
            p.drawText(QPointF(6, SPEECH_Y + 15), "Speech")

        # Range selection.
        if c.range:
            x0, x1 = self.us_to_x(c.range[0]), self.us_to_x(c.range[1])
            bottom = SPEECH_Y + SPEECH_H
            p.fillRect(QRectF(x0, RULER_H, x1 - x0, bottom - RULER_H), QColor(255, 255, 255, 45))
            p.setPen(QPen(QColor(255, 255, 255, 160), 1))
            p.drawLine(QLineF(x0, RULER_H, x0, bottom))
            p.drawLine(QLineF(x1, RULER_H, x1, bottom))

        # Playhead.
        x = self.us_to_x(c.playhead_us)
        if -2 <= x <= w + 2:
            p.setPen(QPen(C_PLAYHEAD, 1))
            p.drawLine(QLineF(x, 0, x, h))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(C_PLAYHEAD)
            p.drawPolygon(QPolygonF([QPointF(x - 5, 0), QPointF(x + 5, 0), QPointF(x, 8)]))
