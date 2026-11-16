"""Right panel: properties of the selected item (media info in Phase 1)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFormLayout, QLabel, QScrollArea, QVBoxLayout, QWidget

from core.media.probe import MediaInfo, format_duration, format_fps, format_size


class PropertiesPanel(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._content = QWidget()
        self._form = QFormLayout(self._content)
        self._form.setContentsMargins(8, 8, 8, 8)
        self._form.setVerticalSpacing(6)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._content)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(scroll)
        self.show_media(None)

    def rows(self) -> dict[str, str]:
        """Current label → value pairs (used by tests)."""
        result: dict[str, str] = {}
        for row in range(self._form.rowCount()):
            label = self._form.itemAt(row, QFormLayout.ItemRole.LabelRole)
            field = self._form.itemAt(row, QFormLayout.ItemRole.FieldRole)
            if label and field:
                result[label.widget().text()] = field.widget().text()
        return result

    def show_media(self, info: MediaInfo | None) -> None:
        while self._form.rowCount():
            self._form.removeRow(0)
        if info is None:
            hint = QLabel("Nothing selected")
            hint.setObjectName("muted")
            self._form.addRow(hint)
            return

        self._section("File")
        self._row("Name", info.path.name)
        self._row("Folder", str(info.path.parent))
        self._row("Size", format_size(info.size_bytes))
        self._row("Container", info.container)
        self._row("Duration", format_duration(info.duration_s))
        if info.bit_rate:
            self._row("Bit rate", f"{info.bit_rate / 1000:.0f} kb/s")

        self._section("Video")
        if info.video:
            v = info.video
            self._row("Codec", v.codec)
            self._row("Resolution", f"{v.width} × {v.height}")
            if v.rotation:
                self._row("Rotation", f"{v.rotation}°")
            fps_text = format_fps(v.avg_fps or v.fps)
            if v.is_vfr_suspected:
                fps_text += " (variable)"
            self._row("Frame rate", fps_text)
            self._row("Pixel format", v.pix_fmt or "?")
        else:
            self._row("Stream", "none")

        self._section(f"Audio ({len(info.audio)})")
        if not info.audio:
            self._row("Stream", "none")
        for n, a in enumerate(info.audio, start=1):
            layout = a.channel_layout or f"{a.channels} ch"
            lang = f" · {a.language}" if a.language else ""
            self._row(f"#{n}", f"{a.codec} · {a.sample_rate} Hz · {layout}{lang}")

    def _section(self, title: str) -> None:
        label = QLabel(title)
        label.setObjectName("sectionTitle")
        self._form.addRow(label)

    def _row(self, name: str, value: str) -> None:
        field = QLabel(value)
        field.setWordWrap(True)
        field.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._form.addRow(QLabel(name), field)
