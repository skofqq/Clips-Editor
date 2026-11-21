"""Publish page cards: post the rendered Short to TikTok or to Instagram Reels.

One :class:`CrossPostPanel` per service, each on its own Publish tab.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QThreadPool, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QWidget,
)

from core.social import tiktok
from core.social.errors import PostCancelled
from core.workers import CancellableTask, Worker
from ui.widgets.m3_progress import WavyProgressBar
from ui.social.guide_panel import INSTAGRAM, TIKTOK
from ui.widgets.card import Card

log = logging.getLogger(__name__)
_HASHTAG = re.compile(r"#\w+", re.UNICODE)


def default_caption(title: str, description: str, limit: int = 2200) -> str:
    """The clip's name and its hashtags: "ОХОТА #sasavot #twitch #shorts"."""
    name = (title or "").split("|")[0].strip()
    tags: list[str] = []
    for tag in _HASHTAG.findall(f"{title} {description}"):
        if tag.casefold() not in (t.casefold() for t in tags):
            tags.append(tag)
    return " ".join([name, *tags]).strip()[:limit]


@dataclass
class CrossPostResult:
    service: str
    ok: bool
    message: str
    url: str = ""


def run_posts(social, path: Path, caption: str, tiktok_post: tiktok.TikTokPost | None, reels: dict | None,
              progress=None, cancel=None) -> list[CrossPostResult]:
    """Post to each chosen service in turn; one failing does not stop the other."""
    results: list[CrossPostResult] = []
    if tiktok_post is not None:
        try:
            r = social.post_tiktok(path, tiktok_post, progress, cancel)
            results.append(CrossPostResult(TIKTOK, True, r.message, r.url))
        except PostCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - shown next to the service
            log.warning("TikTok post failed: %s", exc)
            results.append(CrossPostResult(TIKTOK, False, str(exc)))
    if reels is not None:
        try:
            r = social.post_reel(path, caption, reels.get("share_to_feed", True), reels.get("cover_ms"),
                                 progress, cancel)
            results.append(CrossPostResult(INSTAGRAM, True, "Posted as a Reel.", r.url))
        except PostCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("Instagram post failed: %s", exc)
            results.append(CrossPostResult(INSTAGRAM, False, str(exc)))
    return results


class CrossPostPanel(Card):
    guide_requested = Signal(str)  # service
    posted = Signal(object)  # list[CrossPostResult]

    def __init__(self, social, config, service: str = TIKTOK, parent: QWidget | None = None) -> None:
        is_tiktok = service == TIKTOK
        super().__init__("", ("Post the Short to your profile, or send it to the drafts in the TikTok app."
                              if is_tiktok else "Post the Short as a Reel, with the cover from the Edit page."),
                         parent)
        self.service = service
        self.social = social
        self.config = config
        self.file: Path | None = None
        self.cover_ms = None  # callable giving the thumbnail frame's time in the Short (ms), or None
        self._task: CancellableTask | None = None
        self._info_worker: Worker | None = None
        self._caption_auto = ""

        self.file_label = QLabel(self)
        self.file_label.setObjectName("muted")
        self.file_label.setWordWrap(True)
        self.caption_edit = QPlainTextEdit(self)
        self.caption_edit.setPlaceholderText("Caption with #hashtags")
        self.caption_edit.setFixedHeight(70)
        self.caption_reset = QPushButton("From Data page", self)
        self.caption_reset.setToolTip("Clip title (before “|”) and the hashtags of the title and description")

        self.tiktok_check = QCheckBox("TikTok", self)
        self.tiktok_status = QLabel(self)
        self.tiktok_status.setObjectName("muted")
        self.tiktok_guide = QPushButton("Guide", self)
        self.mode_combo = QComboBox(self)
        self.mode_combo.addItem("Post to the profile", tiktok.DIRECT)
        self.mode_combo.addItem("Send to TikTok drafts", tiktok.INBOX)
        self.mode_combo.setToolTip("Drafts land in the TikTok app's inbox: add a sound and post from the phone. "
                                   "Works before TikTok audits your app.")
        self.privacy_combo = QComboBox(self)
        self._set_privacy_options(("SELF_ONLY",))

        self.reels_check = QCheckBox("Instagram Reels", self)
        self.reels_status = QLabel(self)
        self.reels_status.setObjectName("muted")
        self.reels_guide = QPushButton("Guide", self)
        self.feed_check = QCheckBox("Also show in the feed", self)

        self.post_button = QPushButton("Post to TikTok" if is_tiktok else "Post the Reel", self)
        self.post_button.setObjectName("primary")
        self.cancel_button = QPushButton("Cancel", self)
        self.cancel_button.hide()
        self.progress = WavyProgressBar(self)
        self.progress.setRange(0, 1000)
        self.progress.hide()
        self.result_label = QLabel(self)
        self.result_label.setWordWrap(True)
        self.result_label.setOpenExternalLinks(True)

        # Account row: who is connected, and the set-up guide.
        account = QHBoxLayout()
        account.addWidget(QLabel("Account:", self))
        status = self.tiktok_status if is_tiktok else self.reels_status
        account.addWidget(status, 1)
        account.addWidget(self.tiktok_guide if is_tiktok else self.reels_guide)
        self.body.addLayout(account)
        self.body.addWidget(self.file_label)
        caption_row = QHBoxLayout()
        caption_row.addWidget(self.caption_edit, 1)
        caption_row.addWidget(self.caption_reset, 0, Qt.AlignmentFlag.AlignTop)
        self.body.addLayout(caption_row)
        options = QFormLayout()  # under the caption (the card's own form sits above everything)
        options.setHorizontalSpacing(12)
        options.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        if is_tiktok:
            options.addRow("Where", self.mode_combo)
            options.addRow("Who can see", self.privacy_combo)
        else:
            options.addRow(self.feed_check)
        self.body.addLayout(options)
        others = ((self.reels_status, self.reels_guide, self.feed_check) if is_tiktok
                  else (self.tiktok_status, self.tiktok_guide, self.mode_combo, self.privacy_combo))
        for widget in (*others, self.tiktok_check, self.reels_check):  # the tab is the choice
            widget.hide()
        buttons = QHBoxLayout()
        buttons.addWidget(self.progress, 1)
        buttons.addStretch(0)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.post_button)
        self.body.addLayout(buttons)
        self.body.addWidget(self.result_label)

        defaults = config.social_defaults
        self.mode_combo.setCurrentIndex(max(0, self.mode_combo.findData(defaults.get("tiktok_mode", tiktok.DIRECT))))
        self.feed_check.setChecked(bool(defaults.get("share_to_feed", True)))
        self.tiktok_check.setChecked(is_tiktok)  # one service per panel
        self.reels_check.setChecked(not is_tiktok)

        self.tiktok_guide.clicked.connect(lambda: self.guide_requested.emit(TIKTOK))
        self.reels_guide.clicked.connect(lambda: self.guide_requested.emit(INSTAGRAM))
        self.caption_reset.clicked.connect(lambda: self.caption_edit.setPlainText(self._caption_auto))
        self.post_button.clicked.connect(self.post)
        self.cancel_button.clicked.connect(self.cancel)
        for widget in (self.tiktok_check, self.reels_check):
            widget.toggled.connect(lambda _on: self._update())
        self.mode_combo.currentIndexChanged.connect(lambda _i: self._update())
        self.refresh_accounts()

    # ------------------------------------------------------------ state
    def set_file(self, path: Path | None) -> None:
        self.file = Path(path) if path else None
        self.file_label.setText(f"File: {self.file}" if self.file else "Render the Short first (Render page).")
        self._update()

    def set_caption_source(self, title: str, description: str) -> None:
        """New default caption; replaces the text only when it was not edited."""
        auto = default_caption(title, description)
        if self.caption_edit.toPlainText() in ("", self._caption_auto):
            self.caption_edit.setPlainText(auto)
        self._caption_auto = auto

    def refresh_accounts(self) -> None:
        tiktok_name, reels_name = self.social.tiktok_name, self.social.instagram_name
        self.tiktok_status.setText(tiktok_name or "Not set up")
        self.reels_status.setText(reels_name or "Not set up")
        if tiktok_name and self.service == TIKTOK and self._info_worker is None:
            worker = Worker(lambda: tiktok.creator_info(self.social.tiktok_access_token()))
            worker.signals.finished.connect(self._creator_info)
            worker.signals.failed.connect(lambda exc: self._info_failed(exc))
            self._info_worker = worker
            QThreadPool.globalInstance().start(worker)
        self._update()

    def _creator_info(self, info: tiktok.CreatorInfo) -> None:
        self._info_worker = None
        self._set_privacy_options(info.privacy_options)
        if info.username:
            accounts = self.config.social_accounts
            self.config.social_accounts = {**accounts, "tiktok": {**(accounts.get("tiktok") or {}),
                                                                  "username": info.username}}
            self.tiktok_status.setText(f"@{info.username}")

    def _info_failed(self, exc) -> None:
        self._info_worker = None
        log.info("TikTok creator info: %s", exc)

    def _set_privacy_options(self, options) -> None:
        wanted = self.privacy_combo.currentData() or self.config.social_defaults.get("tiktok_privacy", "SELF_ONLY")
        self.privacy_combo.clear()
        for option in options:
            self.privacy_combo.addItem(tiktok.PRIVACY_TEXT.get(option, option), option)
        self.privacy_combo.setCurrentIndex(max(0, self.privacy_combo.findData(wanted)))

    def _update(self) -> None:
        busy = self._task is not None
        tiktok_ready, reels_ready = bool(self.social.tiktok_name), bool(self.social.instagram_name)
        self.mode_combo.setEnabled(tiktok_ready and not busy)
        self.privacy_combo.setEnabled(tiktok_ready and not busy and self.mode_combo.currentData() == tiktok.DIRECT)
        self.feed_check.setEnabled(reels_ready and not busy)
        chosen = tiktok_ready if self.service == TIKTOK else reels_ready
        self.post_button.setEnabled(not busy and chosen and self.file is not None and self.file.is_file())
        self.post_button.setVisible(not busy)
        self.cancel_button.setVisible(busy)
        self.progress.setVisible(busy)

    @property
    def busy(self) -> bool:
        return self._task is not None

    # ------------------------------------------------------------ posting
    def post(self) -> None:
        if self._task is not None or self.file is None:
            return
        caption = self.caption_edit.toPlainText().strip()
        cover = self.cover_ms() if callable(self.cover_ms) else None
        tiktok_post = None
        if self.social.tiktok_name and self.service == TIKTOK:
            tiktok_post = tiktok.TikTokPost(caption=caption, mode=self.mode_combo.currentData(),
                                            privacy=self.privacy_combo.currentData() or "SELF_ONLY",
                                            cover_ms=cover or 0)
        reels = None
        if self.social.instagram_name and self.service == INSTAGRAM:
            reels = {"share_to_feed": self.feed_check.isChecked(), "cover_ms": cover}
        if self.service == TIKTOK:
            mine = {"tiktok_mode": self.mode_combo.currentData(), "tiktok_privacy": self.privacy_combo.currentData()}
        else:
            mine = {"share_to_feed": self.feed_check.isChecked()}
        self.config.social_defaults = {**self.config.social_defaults, **mine}
        task = CancellableTask(run_posts, self.social, self.file, caption, tiktok_post, reels)
        task.signals.progress.connect(self._progress)
        task.signals.finished.connect(self._done)
        task.signals.failed.connect(lambda exc: self._failed(str(exc)))
        task.signals.cancelled.connect(lambda: self._failed("Cancelled. Anything already posted stays posted."))
        self._task = task
        self.progress.setValue(0)
        self.result_label.setText("Starting…")
        self._update()
        QThreadPool.globalInstance().start(task)

    def cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()

    def _progress(self, fraction: float, message: str) -> None:
        self.progress.setValue(int(fraction * 1000))
        self.result_label.setText(message)

    def _done(self, results: list[CrossPostResult]) -> None:
        self._task = None
        lines = []
        for r in results:
            name = "TikTok" if r.service == TIKTOK else "Instagram"
            mark = "✓" if r.ok else "✗"
            link = f" <a href='{html.escape(r.url)}'>{html.escape(r.url)}</a>" if r.url else ""
            lines.append(f"{mark} <b>{name}</b>: {html.escape(r.message)}{link}")
        self.result_label.setText("<br>".join(lines))
        self._update()
        self.posted.emit(results)

    def _failed(self, message: str) -> None:
        self._task = None
        self.result_label.setText(html.escape(message))
        self._update()
