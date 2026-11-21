"""Help → About: the app's logo, version and who made it (with a GitHub link)."""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication, QIcon, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from app.config import APP_NAME, APP_VERSION
from ui.theme import ICON_DIR, palette

DEVELOPER = "skof"
GITHUB_LOGIN = "skofqq"
GITHUB_URL = f"https://github.com/{GITHUB_LOGIN}"
AVATAR = ICON_DIR / "developer.png"


def round_pixmap(source: QPixmap, size: int) -> QPixmap:
    """``source`` scaled into a circle ``size`` px wide (sharp on high-DPI screens)."""
    ratio = 2.0
    result = QPixmap(int(size * ratio), int(size * ratio))
    result.setDevicePixelRatio(ratio)
    result.fill(Qt.GlobalColor.transparent)
    if source.isNull():
        return result
    scaled = source.scaled(QSize(int(size * ratio), int(size * ratio)), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                           Qt.TransformationMode.SmoothTransformation)
    scaled.setDevicePixelRatio(ratio)
    painter = QPainter(result)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addEllipse(QRectF(0, 0, size, size))
    painter.setClipPath(path)
    painter.drawPixmap(0, 0, scaled)
    painter.end()
    return result


class AboutDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"About {APP_NAME}")
        self.setMinimumWidth(420)
        logo = QLabel(self)
        logo.setPixmap(QIcon(str(ICON_DIR / "app.svg")).pixmap(QSize(96, 96)))
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        name = QLabel(APP_NAME, self)
        name.setObjectName("pageTitle")
        name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.version_label = QLabel(f"Version {APP_VERSION}", self)
        self.version_label.setObjectName("muted")
        self.version_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.version_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        about = QLabel("Turns Twitch clips into vertical Shorts: cut, layout, subtitles, censor, "
                       "cover, render and publish.", self)
        about.setWordWrap(True)
        about.setAlignment(Qt.AlignmentFlag.AlignCenter)

        developer = QFrame(self)
        developer.setObjectName("developerCard")
        self.avatar = QLabel(developer)
        self.avatar.setPixmap(round_pixmap(QPixmap(str(AVATAR)), 56))
        self.avatar.setFixedSize(56, 56)
        who = QLabel(f'<b>{DEVELOPER}</b><br><span style="color:{palette.TEXT_TERTIARY}">Developer</span>',
                     developer)
        link_color = palette.accent().text
        self.github_link = QLabel(f'<a href="{GITHUB_URL}" style="color:{link_color};text-decoration:none">'
                                  f'github.com/{GITHUB_LOGIN}</a>', developer)
        self.github_link.setOpenExternalLinks(True)
        self.github_link.setToolTip(GITHUB_URL)
        text = QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(who)
        text.addWidget(self.github_link)
        row = QHBoxLayout(developer)
        row.setContentsMargins(12, 10, 12, 10)
        row.setSpacing(12)
        row.addWidget(self.avatar)
        row.addLayout(text, 1)

        self.github_button = QPushButton("Open GitHub", self)
        self.github_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(GITHUB_URL)))
        copy = QPushButton("Copy version", self)
        copy.setToolTip("Copy the name and version, for example for a bug report")
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(f"{APP_NAME} {APP_VERSION}"))
        close = QPushButton("Close", self)
        close.setObjectName("primary")
        close.clicked.connect(self.accept)
        buttons = QHBoxLayout()
        buttons.addWidget(self.github_button)
        buttons.addWidget(copy)
        buttons.addStretch(1)
        buttons.addWidget(close)

        column = QVBoxLayout(self)
        column.setContentsMargins(24, 22, 24, 18)
        column.setSpacing(8)
        column.addWidget(logo)
        column.addWidget(name)
        column.addWidget(self.version_label)
        column.addSpacing(4)
        column.addWidget(about)
        column.addSpacing(8)
        column.addWidget(developer)
        column.addSpacing(8)
        column.addLayout(buttons)
