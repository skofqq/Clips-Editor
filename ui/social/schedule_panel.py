"""Publish page "Schedule" tab: ready Shorts spread over the week.

The week at a glance on top, the queue under it, the slot rule (days and
times) and the upload controls. Uploading sends each Short to YouTube as
private with its slot as the publish time, one after another; YouTube makes
it public at the slot. While the app is open it keeps uploading by itself
(and waits for the next day when the daily API quota runs out), and posts to
TikTok / Instagram at the slot time when those are switched on.
"""

from __future__ import annotations

import html
import logging
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from PySide6.QtCore import QDateTime, QRectF, QSize, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.publishing import schedule as sc
from core.workers import CancellableTask
from core.youtube.errors import YouTubeAuthError, YouTubeError, YouTubeQuotaError
from core.youtube.metadata import PRIVATE, VideoMetadata, parse_tags
from ui.theme import palette
from ui.widgets.card import Card
from ui.widgets.m3_progress import ProgressCard

log = logging.getLogger(__name__)

ROLE = Qt.ItemDataRole.UserRole + 1
SERVICE_TEXT = {sc.TIKTOK: "TikTok", sc.INSTAGRAM: "Reels"}
TICK_MS = 60_000


def _when_text(item: sc.QueueItem) -> str:
    m = item.moment
    if m is None:
        return "—"
    local = m.astimezone()
    return f"{sc.DAY_NAMES[local.weekday()]} {local:%d.%m %H:%M}"


def video_metadata(item: sc.QueueItem) -> VideoMetadata:
    """What YouTube gets: private now, public at the slot."""
    m = item.moment
    return VideoMetadata(
        title=item.title, description=item.description, tags=tuple(item.tags), category_id=item.category_id or "20",
        privacy=PRIVATE, publish_at=m.astimezone(timezone.utc) if m is not None else None,
        language=item.language, made_for_kids=item.made_for_kids)


def _status_color(item: sc.QueueItem, now: datetime) -> str:
    if item.status == sc.FAILED:
        return palette.CRITICAL
    if item.status == sc.SCHEDULED:
        return palette.TEXT_TERTIARY if item.is_out(now) else palette.SUCCESS
    if item.status == sc.UPLOADING:
        return palette.CAUTION
    return palette.accent().fill


class WeekView(QWidget):
    """Seven day columns with a chip per Short: time and title, coloured by state."""

    item_clicked = Signal(str)  # item id
    CHIP_H = 34

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.monday = date.today()  # first day shown (any weekday)
        self.days: list[list[sc.QueueItem]] = [[] for _ in range(7)]
        self.slots: list[list[str]] = [[] for _ in range(7)]  # empty slots of the rule, "HH:MM"
        self.selected = ""
        self._chips: list[tuple[QRectF, str]] = []
        self.setMinimumHeight(180)
        self.setMouseTracking(True)

    def set_week(self, monday: date, items: list[sc.QueueItem], rule: sc.Rule) -> None:
        self.monday = monday
        self.days = sc.week(items, monday)
        now = datetime.now().astimezone()
        self.slots = [[] for _ in range(7)]
        start = max(now + sc.LEAD, datetime.combine(monday, datetime.min.time()).astimezone())
        end = datetime.combine(monday + timedelta(days=7), datetime.min.time()).astimezone()
        used = {(i.moment.astimezone().date(), f"{i.moment.astimezone():%H:%M}") for d in self.days for i in d}
        for slot in rule.slots(start, limit=7 * max(1, len(rule.times))):
            if slot >= end:
                break
            key = (slot.date(), f"{slot:%H:%M}")
            if key not in used:
                self.slots[(slot.date() - monday).days].append(key[1])
        rows = max([len(d) + len(s) for d, s in zip(self.days, self.slots)] + [2])
        self.setMinimumHeight(44 + rows * (self.CHIP_H + 6) + 8)
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(640, self.minimumHeight())

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        now = datetime.now().astimezone()
        gap = 6.0
        width = (self.width() - gap * 6) / 7
        small = QFont(self.font())
        small.setPointSizeF(max(7.0, self.font().pointSizeF() - 1))
        bold = QFont(self.font())
        bold.setBold(True)
        self._chips = []
        for n in range(7):
            day = self.monday + timedelta(days=n)
            x = n * (width + gap)
            column = QRectF(x, 0, width, self.height() - 1)
            today = day == now.date()
            path = QPainterPath()
            path.addRoundedRect(column, 10, 10)
            p.fillPath(path, QColor(palette.accent().soft if today else palette.LAYER_ALT))
            p.setPen(QPen(QColor(palette.CARD_STROKE), 1))
            p.drawPath(path)
            p.setFont(bold)
            p.setPen(QColor(palette.accent().text if today else palette.TEXT))
            p.drawText(QRectF(x + 8, 6, width - 16, 18), Qt.AlignmentFlag.AlignLeft,
                       "Today" if today else sc.DAY_NAMES[day.weekday()])
            p.setFont(small)
            p.setPen(QColor(palette.TEXT_SECONDARY))
            p.drawText(QRectF(x + 8, 6, width - 16, 18), Qt.AlignmentFlag.AlignRight, f"{day:%d.%m}")
            y = 32.0
            entries = [(f"{i.moment.astimezone():%H:%M}", i) for i in self.days[n]]
            entries += [(t, None) for t in self.slots[n]]
            entries.sort(key=lambda e: e[0])
            for clock, item in entries:
                chip = QRectF(x + 5, y, width - 10, self.CHIP_H)
                chip_path = QPainterPath()
                chip_path.addRoundedRect(chip, 7, 7)
                if item is None:  # a free slot of the rule
                    pen = QPen(QColor(palette.STROKE_STRONG), 1, Qt.PenStyle.DashLine)
                    p.setPen(pen)
                    p.drawPath(chip_path)
                    p.setPen(QColor(palette.TEXT_TERTIARY))
                    p.drawText(chip.adjusted(7, 2, -4, -2), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                               clock)
                    p.drawText(chip.adjusted(7, 2, -4, -2),
                               Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom, "free")
                else:
                    color = QColor(_status_color(item, now))
                    fill = QColor(color)
                    fill.setAlpha(60)
                    p.fillPath(chip_path, fill)
                    if item.id == self.selected:
                        p.setPen(QPen(QColor(palette.TEXT), 2))
                        p.drawPath(chip_path)
                    p.fillRect(QRectF(chip.x(), chip.y() + 5, 3, chip.height() - 10), color)
                    p.setPen(QColor(palette.TEXT))
                    text_rect = chip.adjusted(8, 2, -4, -2)
                    mark = " 📌" if item.pinned and item.movable else ""
                    p.drawText(text_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, clock + mark)
                    p.setPen(QColor(palette.TEXT_SECONDARY))
                    title = QFontMetrics(small).elidedText(item.title, Qt.TextElideMode.ElideRight,
                                                           int(text_rect.width()))
                    p.drawText(text_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom, title)
                    self._chips.append((chip, item.id))
                y += self.CHIP_H + 6
        p.end()

    def chip_at(self, x: float, y: float) -> str:
        for rect, item_id in self._chips:
            if rect.contains(x, y):
                return item_id
        return ""

    def mousePressEvent(self, event) -> None:  # noqa: N802
        item_id = self.chip_at(event.position().x(), event.position().y())
        if item_id:
            self.selected = item_id
            self.update()
            self.item_clicked.emit(item_id)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        hit = self.chip_at(event.position().x(), event.position().y())
        self.setCursor(Qt.CursorShape.PointingHandCursor if hit else Qt.CursorShape.ArrowCursor)


class ItemDialog(QDialog):
    """Change a waiting Short: title, description, tags and a fixed time."""

    def __init__(self, item: sc.QueueItem, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Scheduled Short")
        self.setMinimumWidth(520)
        self.item = item
        self.title_edit = QLineEdit(item.title, self)
        self.description_edit = QPlainTextEdit(item.description, self)
        self.description_edit.setMinimumHeight(110)
        self.tags_edit = QLineEdit(", ".join(item.tags), self)
        self.fixed_check = QCheckBox("Fixed time", self)
        self.fixed_check.setToolTip("Off: the Short takes the next free slot in queue order")
        self.time_edit = QDateTimeEdit(self)
        self.time_edit.setCalendarPopup(True)
        self.time_edit.setDisplayFormat("dd.MM.yyyy HH:mm")
        m = item.moment
        start = m.astimezone().replace(tzinfo=None) if m is not None else datetime.now() + timedelta(hours=2)
        self.time_edit.setDateTime(QDateTime(start.replace(second=0, microsecond=0)))
        self.fixed_check.setChecked(item.pinned)
        self.time_edit.setEnabled(item.pinned)
        self.fixed_check.toggled.connect(self.time_edit.setEnabled)
        form = QFormLayout()
        form.addRow("Title", self.title_edit)
        form.addRow("Description", self.description_edit)
        form.addRow("Tags", self.tags_edit)
        row = QHBoxLayout()
        row.addWidget(self.fixed_check)
        row.addWidget(self.time_edit, 1)
        form.addRow("When", row)
        file_label = QLabel(item.file, self)
        file_label.setObjectName("muted")
        file_label.setWordWrap(True)
        form.addRow("File", file_label)
        self.problems_label = QLabel(self)
        self.problems_label.setWordWrap(True)
        self.problems_label.setStyleSheet(f"color: {palette.CRITICAL}")
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.problems_label)
        layout.addWidget(buttons)
        self.problems_label.hide()

    def result_item(self) -> sc.QueueItem:
        item = replace(self.item, title=self.title_edit.text().strip(),
                       description=self.description_edit.toPlainText(),
                       tags=parse_tags(self.tags_edit.text()), pinned=self.fixed_check.isChecked())
        if item.pinned:
            local = self.time_edit.dateTime().toPython().astimezone()
            item = replace(item, when=local.isoformat(timespec="minutes"))
        return item

    def _accept(self) -> None:
        item = self.result_item()
        meta = video_metadata(replace(item, when=""))
        issues = meta.problems()
        if item.pinned and item.moment is not None and item.moment < datetime.now().astimezone() + sc.LEAD:
            issues.append("Pick a time at least 30 minutes from now.")
        if issues:
            self.problems_label.setText("\n".join(issues))
            self.problems_label.show()
            return
        self.accept()


def _upload_one(client, item: sc.QueueItem, cover: Path | None, progress=None, cancel=None):
    from ui.youtube.upload_dialog import _upload

    thumbnail = (lambda: cover.read_bytes()) if cover is not None else None
    return _upload(client, item.file, video_metadata(item), progress, cancel, item.captions_srt,
                   item.captions_language, thumbnail)


class SchedulePanel(Card):
    uploaded = Signal(object, object)  # QueueItem, UploadResult
    posted = Signal(object, str, str)  # QueueItem, service (tiktok / instagram), url
    changed = Signal()

    def __init__(self, store: sc.PublishSchedule, youtube, social, config, parent: QWidget | None = None) -> None:
        super().__init__("Publishing schedule", "Put ready Shorts in the queue (YouTube tab → Add to schedule) and "
                         "they take the next free slots. Each one goes up to YouTube early as private, and YouTube "
                         "makes it public at its time, even when this computer is off.", parent)
        self.store = store
        self.youtube = youtube
        self.social = social
        self.config = config
        self.best_time = None
        self._task: CancellableTask | None = None
        self._run_all = False  # keep going after this upload
        self._post_task: CancellableTask | None = None
        self.monday = date.today()  # the seven days shown start here

        # ------------------------------------------------------ rule
        self.day_buttons: list[QPushButton] = []
        days = QHBoxLayout()
        days.setSpacing(4)
        for n, name in enumerate(sc.DAY_NAMES):
            button = QPushButton(name, self)
            button.setCheckable(True)
            button.setObjectName("chip")
            button.setMinimumWidth(58)
            days.addWidget(button)
            self.day_buttons.append(button)
        days.addStretch(1)
        self.times_edit = QLineEdit(self)
        self.times_edit.setPlaceholderText("18:00, 21:00")
        self.times_edit.setToolTip("Times of day, separated by commas. Each time is one Short a day.")
        self.best_button = QPushButton("Best time", self)
        self.best_button.setToolTip("Use the time of day your channel's Shorts got the most views")
        self.best_button.setEnabled(False)
        times = QHBoxLayout()
        times.addWidget(self.times_edit, 1)
        times.addWidget(self.best_button)
        self.rule_label = QLabel(self)
        self.rule_label.setObjectName("muted")
        self.rule_label.setWordWrap(True)
        self.tiktok_check = QCheckBox("TikTok", self)
        self.reels_check = QCheckBox("Instagram Reels", self)
        also = QHBoxLayout()
        also.setSpacing(14)
        also.addWidget(self.tiktok_check)
        also.addWidget(self.reels_check)
        also_note = QLabel("posted by the app at the slot time, so it has to be open then", self)
        also_note.setObjectName("muted")
        also.addWidget(also_note, 1)
        rule_form = QFormLayout()
        rule_form.setHorizontalSpacing(12)
        rule_form.addRow("Days", days)
        rule_form.addRow("Times", times)
        rule_form.addRow("", self.rule_label)
        rule_form.addRow("Also post to", also)
        self.body.addLayout(rule_form)

        # ------------------------------------------------------ week
        self.prev_button = QPushButton("‹", self)
        self.next_button = QPushButton("›", self)
        self.today_button = QPushButton("From today", self)
        for b in (self.prev_button, self.next_button):
            b.setFixedWidth(36)
        self.week_label = QLabel(self)
        self.week_label.setObjectName("cardTitle")
        week_bar = QHBoxLayout()
        week_bar.addWidget(self.prev_button)
        week_bar.addWidget(self.week_label, 1, Qt.AlignmentFlag.AlignCenter)
        week_bar.addWidget(self.today_button)
        week_bar.addWidget(self.next_button)
        self.body.addLayout(week_bar)
        self.week_view = WeekView(self)
        self.body.addWidget(self.week_view)

        # ------------------------------------------------------ queue
        self.list = QTreeWidget(self)
        self.list.setHeaderLabels(["When", "Short", "State"])
        self.list.setRootIsDecorated(False)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setMinimumHeight(210)
        header = self.list.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.body.addWidget(self.list)
        self.empty_label = QLabel("The queue is empty. Render a Short, open the YouTube tab and press "
                                  "“Add to schedule”, or add rendered files here.", self)
        self.empty_label.setObjectName("muted")
        self.empty_label.setWordWrap(True)
        self.body.addWidget(self.empty_label)
        self.add_button = QPushButton("Add files…", self)
        self.up_button = QPushButton("Earlier", self)
        self.down_button = QPushButton("Later", self)
        self.edit_button = QPushButton("Edit…", self)
        self.remove_button = QPushButton("Remove", self)
        queue_buttons = QHBoxLayout()
        for b in (self.add_button, self.up_button, self.down_button, self.edit_button, self.remove_button):
            queue_buttons.addWidget(b)
        queue_buttons.addStretch(1)
        self.body.addLayout(queue_buttons)

        # ------------------------------------------------------ upload
        self.auto_check = QCheckBox("Upload by itself while the app is open", self)
        self.auto_check.setToolTip("Uploads waiting Shorts in the background, and after YouTube's daily limit "
                                   "goes on the next day")
        self.upload_button = QPushButton("Upload now", self)
        self.upload_button.setObjectName("primary")
        self.cancel_button = QPushButton("Stop", self)
        self.cancel_button.hide()
        self.progress_card = ProgressCard("Uploading to YouTube", self)
        self.progress_card.hide()
        self.summary_label = QLabel(self)
        self.summary_label.setObjectName("muted")
        self.summary_label.setWordWrap(True)
        self.status_label = QLabel(self)  # what just happened
        self.status_label.setWordWrap(True)
        self.status_label.setOpenExternalLinks(True)
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        upload_row = QHBoxLayout()
        upload_row.addWidget(self.auto_check, 1)
        upload_row.addWidget(self.cancel_button)
        upload_row.addWidget(self.upload_button)
        self.body.addWidget(self.summary_label)
        self.body.addWidget(self.progress_card)
        self.body.addWidget(self.status_label)
        self.body.addLayout(upload_row)

        # ------------------------------------------------------ state
        self._load_rule()
        self.auto_check.setChecked(store.auto_upload)
        self.tiktok_check.setChecked(sc.TIKTOK in store.services)
        self.reels_check.setChecked(sc.INSTAGRAM in store.services)
        for button in self.day_buttons:
            button.toggled.connect(lambda _on: self._rule_changed())
        self.times_edit.editingFinished.connect(self._rule_changed)
        self.best_button.clicked.connect(self.use_best_time)
        self.tiktok_check.toggled.connect(lambda _on: self._services_changed())
        self.reels_check.toggled.connect(lambda _on: self._services_changed())
        self.prev_button.clicked.connect(lambda: self.show_week(self.monday - timedelta(days=7)))
        self.next_button.clicked.connect(lambda: self.show_week(self.monday + timedelta(days=7)))
        self.today_button.clicked.connect(lambda: self.show_week(date.today()))
        self.week_view.item_clicked.connect(self.select)
        self.list.currentItemChanged.connect(lambda *_a: self._selection_changed())
        self.list.itemDoubleClicked.connect(lambda *_a: self.edit_selected())
        self.add_button.clicked.connect(self.add_files)
        self.up_button.clicked.connect(lambda: self._move(-1))
        self.down_button.clicked.connect(lambda: self._move(1))
        self.edit_button.clicked.connect(self.edit_selected)
        self.remove_button.clicked.connect(self.remove_selected)
        self.auto_check.toggled.connect(self._auto_changed)
        self.upload_button.clicked.connect(self.upload_all)
        self.cancel_button.clicked.connect(self.cancel)
        self.timer = QTimer(self)
        self.timer.setInterval(TICK_MS)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        self.store.prune()
        self.store.arrange()
        self.refresh()
        QTimer.singleShot(15_000, self.tick)  # after start-up settles

    # ------------------------------------------------------------ rule
    def _load_rule(self) -> None:
        rule = self.store.rule
        for n, button in enumerate(self.day_buttons):
            button.blockSignals(True)
            button.setChecked(n in rule.days)
            button.blockSignals(False)
        self.times_edit.setText(", ".join(rule.times))
        self._show_rule()

    def _show_rule(self) -> None:
        rule = self.store.rule
        if not rule.per_week:
            self.rule_label.setText("Choose at least one day and one time.")
            return
        per_day = len(rule.times)
        text = f"{rule.text}: {rule.per_week} Short{'s' if rule.per_week != 1 else ''} a week."
        if per_day > sc.UPLOADS_PER_DAY:
            text += (f" YouTube's free API quota allows about {sc.UPLOADS_PER_DAY} uploads a day, so some "
                     "will go up a day later than planned.")
        self.rule_label.setText(text)

    def rule(self) -> sc.Rule:
        days = tuple(n for n, b in enumerate(self.day_buttons) if b.isChecked())
        return sc.Rule(days, sc.parse_times(self.times_edit.text()))

    def _rule_changed(self) -> None:
        rule = self.rule()
        self.times_edit.setText(", ".join(rule.times))
        if rule == self.store.rule:
            return
        self.store.set_rule(rule)
        self._show_rule()
        self.refresh()

    def set_best_time(self, best) -> None:
        """core.youtube.best_time.BestTime of the channel, or None."""
        self.best_time = best
        self.best_button.setEnabled(best is not None)
        if best is not None:
            self.best_button.setToolTip(f"Your Shorts did best at {best.window_text} "
                                        f"({best.lift:.1f}× the usual views)")

    def use_best_time(self) -> None:
        if self.best_time is None:
            return
        best = f"{self.best_time.start_hour:02d}:00"
        times = sc.parse_times(self.times_edit.text())
        if best in times:
            return
        if len(times) <= 1:  # one Short a day: move it to the best time
            times = (best,)
        else:  # several: the one nearest to the best time moves there
            hour = self.best_time.start_hour * 60
            nearest = min(times, key=lambda t: abs(int(t[:2]) * 60 + int(t[3:]) - hour))
            times = tuple(best if t == nearest else t for t in times)
        self.times_edit.setText(", ".join(sorted(times)))
        self._rule_changed()

    def _services_changed(self) -> None:
        services = tuple(s for s, check in ((sc.TIKTOK, self.tiktok_check), (sc.INSTAGRAM, self.reels_check))
                         if check.isChecked())
        self.store.set_services(services)
        self.refresh()

    def _auto_changed(self, on: bool) -> None:
        self.store.auto_upload = bool(on)
        self.store.save()
        if on:
            QTimer.singleShot(0, self.tick)

    # ------------------------------------------------------------ view
    def show_week(self, monday: date) -> None:
        self.monday = monday
        self.refresh()

    def _show_day(self, day: date) -> None:
        """Keep the shown seven days when ``day`` is among them, else jump by whole weeks."""
        while day < self.monday:
            self.monday -= timedelta(days=7)
        while day >= self.monday + timedelta(days=7):
            self.monday += timedelta(days=7)
        self.refresh()

    def refresh(self) -> None:
        now = datetime.now().astimezone()
        items = sc.ordered(self.store.items)
        current = self.selected_id()
        self.list.blockSignals(True)
        self.list.clear()
        for item in items:
            row = QTreeWidgetItem([_when_text(item) + (" 📌" if item.pinned and item.movable else ""),
                                   item.title or Path(item.file).name, self._state_text(item, now)])
            row.setData(0, ROLE, item.id)
            row.setToolTip(1, item.file)
            row.setToolTip(2, item.error or item.url)
            row.setForeground(2, QColor(_status_color(item, now)))
            self.list.addTopLevelItem(row)
            if item.id == current:
                self.list.setCurrentItem(row)
        self.list.blockSignals(False)
        self.list.setVisible(bool(items))
        self.empty_label.setVisible(not items)
        end = self.monday + timedelta(days=6)
        self.week_label.setText(f"{self.monday:%d.%m} – {end:%d.%m.%Y}")
        self.week_view.selected = current
        self.week_view.set_week(self.monday, self.store.items, self.store.rule)
        self._update_buttons()
        self._show_summary(now)

    def _state_text(self, item: sc.QueueItem, now: datetime) -> str:
        text = item.status_text(now)
        posts = []
        for service, name in SERVICE_TEXT.items():
            state = (item.posts.get(service) or {}).get("state")
            if state == sc.POST_DONE:
                posts.append(f"{name} ✓")
            elif state == sc.POST_FAILED:
                posts.append(f"{name} ✗")
            elif state == sc.POST_WAIT:
                posts.append(f"{name} at the time")
        if not Path(item.file).is_file() and item.movable:
            text = "File is missing"
        return " · ".join([text, *posts])

    def _show_summary(self, now: datetime) -> None:
        waiting = [i for i in self.store.items if i.status == sc.WAITING]
        scheduled = [i for i in self.store.items if i.status == sc.SCHEDULED and not i.is_out(now)]
        parts = []
        if scheduled:
            parts.append(f"{len(scheduled)} on YouTube waiting for their time")
        if waiting:
            parts.append(f"{len(waiting)} to upload")
            unplaced = [i for i in waiting if not i.when]
            if unplaced:
                parts.append(f"{len(unplaced)} without a slot (add days or times)")
        if self.store.paused(now):
            until = datetime.fromisoformat(self.store.paused_until).astimezone()
            parts.append(f"YouTube's daily limit is used up; uploading again after {until:%d.%m %H:%M}")
        if waiting and self.youtube.active is None:
            parts.append("connect a YouTube channel to upload")
        text = " · ".join(parts)
        self.summary_label.setText(text[:1].upper() + text[1:] + "." if text else "")
        self.summary_label.setVisible(bool(text))

    def selected_id(self) -> str:
        row = self.list.currentItem()
        return row.data(0, ROLE) if row is not None else ""

    def selected(self) -> sc.QueueItem | None:
        return self.store.get(self.selected_id())

    def select(self, item_id: str) -> None:
        for n in range(self.list.topLevelItemCount()):
            row = self.list.topLevelItem(n)
            if row.data(0, ROLE) == item_id:
                self.list.setCurrentItem(row)
                return

    def _selection_changed(self) -> None:
        self.week_view.selected = self.selected_id()
        self.week_view.update()
        self._update_buttons()

    def _update_buttons(self) -> None:
        item = self.selected()
        movable = item is not None and item.movable and item.status != sc.UPLOADING
        busy = self._task is not None
        self.up_button.setEnabled(movable)
        self.down_button.setEnabled(movable)
        self.edit_button.setEnabled(movable)
        self.remove_button.setEnabled(item is not None and not (busy and item.status == sc.UPLOADING))
        waiting = any(i.status in (sc.WAITING, sc.FAILED) and i.when for i in self.store.items)
        self.upload_button.setEnabled(not busy and waiting)
        self.upload_button.setVisible(not busy)
        self.cancel_button.setVisible(busy)

    # ------------------------------------------------------------ queue
    def add(self, item: sc.QueueItem) -> sc.QueueItem:
        item = self.store.add(item)
        m = item.moment
        if m is not None:
            self._show_day(m.astimezone().date())
        else:
            self.refresh()
        self.select(item.id)
        self.changed.emit()
        QTimer.singleShot(0, self.tick)
        return item

    def add_files(self) -> None:
        start = getattr(self.config, "export_dir", "") or ""
        files, _ = QFileDialog.getOpenFileNames(self, "Add rendered Shorts", start,
                                                "Rendered video (*.mp4 *.mkv *.mov *.webm);;All files (*)")
        if not files:
            return
        from ui.youtube.upload_dialog import default_title

        defaults = getattr(self.config, "youtube_defaults", {}) or {}
        for file in files:
            path = Path(file)
            self.store.add(sc.new_item(path, default_title(path), category_id=str(defaults.get("category") or "20"),
                                       language=str(defaults.get("language") or ""),
                                       tags=parse_tags(str(defaults.get("tags", "")))))
        self.refresh()
        self.status_label.setText(f"Added {len(files)} file{'s' if len(files) != 1 else ''}. "
                                  "Check the titles (Edit…) before they go up.")
        self.changed.emit()

    def _move(self, step: int) -> None:
        item = self.selected()
        if item is not None:
            self.store.move(item.id, step)
            self.refresh()

    def edit_selected(self) -> None:
        item = self.selected()
        if item is None or not item.movable or item.status == sc.UPLOADING:
            return
        dialog = ItemDialog(item, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            edited = dialog.result_item()
            if edited.status == sc.FAILED:
                edited = replace(edited, status=sc.WAITING, error="")
            self.store.update(edited, rearrange=True)
            self.refresh()
            m = self.store.get(edited.id).moment if self.store.get(edited.id) else None
            if m is not None:
                self._show_day(m.astimezone().date())
            self.select(edited.id)

    def remove_selected(self) -> None:
        item = self.selected()
        if item is None:
            return
        if item.status == sc.SCHEDULED and not item.is_out():
            text = ("This Short is already on YouTube and goes public at its time. Removing it here only takes "
                    "it off this list: to stop it, delete or reschedule it in YouTube Studio.")
        else:
            text = f"Remove “{item.title}” from the schedule? The video file stays."
        if QMessageBox.question(self, "Schedule", text) != QMessageBox.StandardButton.Yes:
            return
        self.store.remove(item.id)
        self.refresh()
        self.changed.emit()

    # ------------------------------------------------------------ uploading
    @property
    def busy(self) -> bool:
        return self._task is not None or self._post_task is not None

    def upload_all(self) -> None:
        """Upload every waiting Short now (failed ones get another try)."""
        if self._task is not None:
            return
        for item in list(self.store.items):
            if item.status == sc.FAILED:
                self.store.update(replace(item, status=sc.WAITING, error=""))
        self.store.paused_until = ""
        self.store.arrange()
        self.store.save()
        self._run_all = True
        if not self._start_next():
            self._run_all = False
            self.refresh()

    def tick(self) -> None:
        """Every minute: re-place missed slots, upload in the background, post where it is time."""
        now = datetime.now().astimezone()
        if self._task is None:
            before = [i.when for i in self.store.items]
            self.store.arrange(now)
            if [i.when for i in self.store.items] != before:
                self.store.save()
            if self.store.auto_upload and not self.store.paused(now) and self.youtube.active is not None:
                self._run_all = True
                if not self._start_next(quiet=True):
                    self._run_all = False
        if self._post_task is None:
            self._post_due(now)
        self.refresh()

    def _start_next(self, quiet: bool = False) -> bool:
        item = sc.next_upload(self.store.items)
        if item is None:
            if not quiet and not self.store.waiting():
                self.status_label.setText("Nothing waiting to upload.")
            return False
        if not Path(item.file).is_file():
            self.store.update(replace(item, status=sc.FAILED, error="the video file is missing"))
            return self._start_next(quiet)
        channel = next((c for c in self.youtube.accounts if c.id == item.channel_id), None) or self.youtube.active
        if channel is None or not self.youtube.has_token(channel.id):
            if not quiet:
                QMessageBox.warning(self, "Schedule", "Connect a YouTube channel first (YouTube tab → Accounts…).")
            return False
        issues = video_metadata(item).problems()
        if issues:
            self.store.update(replace(item, status=sc.FAILED, error=" ".join(issues)))
            return self._start_next(quiet)
        item = replace(item, status=sc.UPLOADING, channel_id=channel.id, channel_title=channel.title, error="")
        self.store.update(item)
        task = CancellableTask(_upload_one, self.youtube.client_for(channel.id), item, self.store.cover_path(item))
        task.signals.progress.connect(self._progress)
        task.signals.finished.connect(lambda result, i=item.id: self._uploaded(i, result))
        task.signals.failed.connect(lambda exc, i=item.id: self._failed(i, exc))
        task.signals.cancelled.connect(lambda i=item.id: self._cancelled(i))
        self._task = task
        self.progress_card.start(f"Uploading “{item.title}”", f"Goes public {_when_text(item)}")
        self.status_label.setText("")
        self.refresh()
        log.info("Schedule: uploading %s for %s", item.file, item.when)
        QThreadPool.globalInstance().start(task)
        return True

    def cancel(self) -> None:
        self._run_all = False
        if self._task is not None:
            self._task.cancel()
            self.progress_card.title_label.setText("Stopping…")

    def _progress(self, fraction: float, message: str) -> None:
        self.progress_card.set_progress(fraction, message)

    def _done(self) -> None:
        self._task = None
        self.progress_card.hide()

    def _uploaded(self, item_id: str, result) -> None:
        self._done()
        item = self.store.get(item_id)
        if item is None:
            return
        item = replace(item, status=sc.SCHEDULED, video_id=result.video_id, url=result.url)
        self.store.update(item)
        notes = []
        if result.captions and result.captions != "added":
            notes.append(f"captions not added ({result.captions})")
        if result.thumbnail and result.thumbnail != "set":
            notes.append(f"thumbnail not set ({result.thumbnail})")
        if not result.publish_at and result.privacy == PRIVATE:
            notes.append("YouTube kept it private without a publish time: check it in YouTube Studio")
        text = (f"✓ “{html.escape(item.title)}” is on YouTube and goes public {_when_text(item)}: "
                f"<a href='{result.url}'>{result.url}</a>")
        if notes:
            text += "<br>" + html.escape("; ".join(notes).capitalize())
        self.status_label.setText(text)
        self.uploaded.emit(item, result)
        self.changed.emit()
        if not (self._run_all and self._start_next(quiet=True)):
            self._run_all = False
        self.refresh()

    def _failed(self, item_id: str, exc) -> None:
        self._done()
        item = self.store.get(item_id)
        message = str(exc) if isinstance(exc, YouTubeError) else f"Unexpected error: {exc}"
        log.warning("Schedule upload failed: %s", message)
        if isinstance(exc, YouTubeQuotaError):
            until = self.store.pause_for_quota()
            if item is not None:
                self.store.update(replace(item, status=sc.WAITING))
            self._run_all = False
            self.status_label.setText(html.escape(
                f"YouTube's daily limit is reached. The rest go up after {until:%d.%m %H:%M} "
                "(keep the app open with “Upload by itself” on, or press Upload now then)."))
            self.refresh()
            return
        if item is not None:
            self.store.update(replace(item, status=sc.FAILED, error=message))
        if isinstance(exc, YouTubeAuthError):  # every next one would fail the same way
            self._run_all = False
            self.status_label.setText(html.escape(message + " Open Accounts… and reconnect the channel."))
        elif not (self._run_all and self._start_next(quiet=True)):
            self._run_all = False
            self.status_label.setText(html.escape(f"Upload failed: {message}"))
        self.refresh()

    def _cancelled(self, item_id: str) -> None:
        self._done()
        self._run_all = False
        item = self.store.get(item_id)
        if item is not None:
            self.store.update(replace(item, status=sc.WAITING))
        self.store.auto_upload = False  # Stop means stop, also in the background
        self.auto_check.blockSignals(True)
        self.auto_check.setChecked(False)
        self.auto_check.blockSignals(False)
        self.store.save()
        self.status_label.setText("Stopped. Nothing half-uploaded was published. “Upload by itself” is off now.")
        self.refresh()

    # ------------------------------------------------------------ TikTok / Reels at the time
    def _post_due(self, now: datetime) -> None:
        services = tuple(s for s in self.store.services
                         if (s == sc.TIKTOK and self.social.tiktok_name)
                         or (s == sc.INSTAGRAM and self.social.instagram_name))
        due = sc.due_posts(self.store.items, services, now)
        due = [(i, s) for i, s in due if Path(i.file).is_file()]
        if not due:
            return
        item, service = due[0]
        from core.social import tiktok
        from ui.social.crosspost_panel import default_caption, run_posts

        caption = default_caption(item.title, item.description)
        defaults = getattr(self.config, "social_defaults", {}) or {}
        tiktok_post = reels = None
        if service == sc.TIKTOK:
            tiktok_post = tiktok.TikTokPost(caption=caption, mode=defaults.get("tiktok_mode", tiktok.DIRECT),
                                            privacy=defaults.get("tiktok_privacy", "SELF_ONLY"))
        else:
            reels = {"share_to_feed": bool(defaults.get("share_to_feed", True)), "cover_ms": None}
        task = CancellableTask(run_posts, self.social, Path(item.file), caption, tiktok_post, reels)
        task.signals.finished.connect(lambda results, i=item.id, s=service: self._posted(i, s, results))
        task.signals.failed.connect(lambda exc, i=item.id, s=service: self._post_failed(i, s, str(exc)))
        task.signals.cancelled.connect(lambda: setattr(self, "_post_task", None))
        self._post_task = task
        self.store.set_post(item.id, service, sc.POST_SENDING)
        log.info("Schedule: posting %s to %s", item.file, service)
        QThreadPool.globalInstance().start(task)

    def _posted(self, item_id: str, service: str, results) -> None:
        self._post_task = None
        result = results[0] if results else None
        if result is not None and result.ok:
            self.store.set_post(item_id, service, sc.POST_DONE, result.url)
            item = self.store.get(item_id)
            if item is not None:
                self.posted.emit(item, service, result.url)
        else:
            self._post_failed(item_id, service, result.message if result is not None else "no answer")
            return
        self.refresh()
        QTimer.singleShot(0, lambda: self._post_due(datetime.now().astimezone()))

    def _post_failed(self, item_id: str, service: str, message: str) -> None:
        self._post_task = None
        log.warning("Schedule post to %s failed: %s", service, message)
        self.store.set_post(item_id, service, sc.POST_FAILED, error=message)
        self.refresh()
