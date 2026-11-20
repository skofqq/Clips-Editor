"""Data page: how the Short will look on YouTube, updated while you type.

A vertical tile (frame of the clip with the title over it, like the Shorts
feed) and the watch-page text: title, channel, the first lines of the
description with hashtags highlighted, and the tags (not shown publicly)."""

from __future__ import annotations

import html
import re

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QLabel, QSizePolicy, QWidget

from ui.theme import palette
from ui.widgets.card import Card

TILE_W, TILE_H = 216, 384
_HASHTAG = re.compile(r"(#[\w]+)", re.UNICODE)


class ShortTile(QWidget):
    """A 9:16 tile: the clip's frame, dark fade at the bottom, the title over it."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(TILE_W, TILE_H)
        self.title = ""
        self.frame: QPixmap | None = None

    def set_content(self, title: str, frame: QPixmap | None) -> None:
        self.title, self.frame = title, frame
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect())
        clip = QPainterPath()
        clip.addRoundedRect(rect, 12, 12)
        p.setClipPath(clip)
        p.fillRect(rect, QColor("#111111"))
        if self.frame is not None and not self.frame.isNull():
            scaled = self.frame.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                       Qt.TransformationMode.SmoothTransformation)
            x = (scaled.width() - self.width()) // 2
            y = (scaled.height() - self.height()) // 2
            p.drawPixmap(0, 0, scaled, x, y, self.width(), self.height())
        fade = QLinearGradient(0, rect.height() * 0.55, 0, rect.height())
        fade.setColorAt(0, QColor(0, 0, 0, 0))
        fade.setColorAt(1, QColor(0, 0, 0, 200))
        p.fillRect(rect, fade)
        font = QFont(self.font())
        font.setPixelSize(15)
        font.setWeight(QFont.Weight.DemiBold)
        p.setFont(font)
        p.setPen(QColor("#ffffff"))
        text_rect = rect.adjusted(10, rect.height() * 0.6, -10, -12)
        p.drawText(text_rect, Qt.AlignmentFlag.AlignBottom | Qt.TextFlag.TextWordWrap,
                   self.title or "Title of the Short")
        p.end()


class YouTubePreview(Card):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("On YouTube", "Updates while you type.", parent)
        self.tile = ShortTile(self)
        self.body.addWidget(self.tile, 0, Qt.AlignmentFlag.AlignHCenter)
        self.title_label = QLabel(self)
        self.title_label.setObjectName("previewTitle")
        self.title_label.setWordWrap(True)
        self.channel_label = QLabel(self)
        self.channel_label.setObjectName("muted")
        self.description_label = QLabel(self)
        self.description_label.setWordWrap(True)
        self.description_label.setTextFormat(Qt.TextFormat.RichText)
        self.description_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.tags_label = QLabel(self)
        self.tags_label.setObjectName("muted")
        self.tags_label.setWordWrap(True)
        for w in (self.title_label, self.channel_label, self.description_label, self.tags_label):
            w.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
            self.body.addWidget(w)
        self.setFixedWidth(TILE_W + 60)
        self.set_metadata("", "", (), "", None)

    def set_metadata(self, title: str, description: str, tags, channel: str, frame: QPixmap | None) -> None:
        title = title.strip()
        self.tile.set_content(title, frame)
        self.title_label.setText(f"<b>{html.escape(title)}</b>" if title else
                                 f"<span style='color:{palette.TEXT_TERTIARY}'>No title yet</span>")
        self.channel_label.setText(f"@{channel}" if channel else "No channel connected")
        lines = [ln for ln in description.splitlines()][:6]
        more = len(description.splitlines()) > 6
        shown = "<br>".join(_HASHTAG.sub(rf"<span style='color:{palette.accent().fill}'>\1</span>",
                                         html.escape(ln)) for ln in lines)
        if more:
            shown += f"<br><span style='color:{palette.TEXT_TERTIARY}'>…more</span>"
        self.description_label.setText(shown or f"<span style='color:{palette.TEXT_TERTIARY}'>No description</span>")
        tags = list(tags)
        self.tags_label.setText(f"Tags ({len(tags)}, not shown on YouTube): " + ", ".join(tags) if tags else "No tags")
