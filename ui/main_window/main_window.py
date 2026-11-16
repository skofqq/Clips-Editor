"""Main window: docks, menus, import flow and project save/open."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QByteArray, QThreadPool, Qt, Signal
from PySide6.QtGui import QAction, QDragEnterEvent, QDropEvent, QKeySequence
from PySide6.QtWidgets import QDockWidget, QFileDialog, QMainWindow, QMessageBox, QWidget

from app.config import APP_NAME, APP_VERSION, AppConfig
from core.media.errors import MediaError
from core.media.ffmpeg_locator import FFmpegPaths, tool_version
from core.media.probe import SUPPORTED_EXTENSIONS, MediaInfo, probe_media
from core.projects.project import (
    PROJECT_EXTENSION,
    MediaRef,
    Project,
    ProjectFormatError,
    load_project,
    save_project,
)
from core.workers import Worker
from ui.panels.media_panel import MediaPanel
from ui.panels.properties_panel import PropertiesPanel
from ui.panels.timeline_panel import TimelinePanel
from ui.preview.preview_panel import PreviewPanel

log = logging.getLogger(__name__)

_VIDEO_FILTER = "Video files ({});;All files (*)".format(
    " ".join(f"*{ext}" for ext in SUPPORTED_EXTENSIONS)
)


class MainWindow(QMainWindow):
    media_imported = Signal(object)  # MediaInfo
    import_failed = Signal(str, str)  # path, message

    def __init__(self, config: AppConfig, ffmpeg: FFmpegPaths | None) -> None:
        super().__init__()
        self.config = config
        self.ffmpeg = ffmpeg
        self.project = Project()
        self.project_path: Path | None = None
        self._dirty = False
        self._pending: set[Worker] = set()
        self.pool = QThreadPool.globalInstance()

        self.setWindowTitle(APP_NAME)
        self.resize(1440, 900)
        self.setAcceptDrops(True)
        self.setDockNestingEnabled(True)

        self.preview = PreviewPanel(self)
        self.setCentralWidget(self.preview)

        self.media_panel = MediaPanel(self)
        self.properties_panel = PropertiesPanel(self)
        self.timeline_panel = TimelinePanel(self)

        self.media_dock = self._add_dock("Media", "dock_media", self.media_panel, Qt.DockWidgetArea.LeftDockWidgetArea)
        self.properties_dock = self._add_dock(
            "Properties", "dock_properties", self.properties_panel, Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.timeline_dock = self._add_dock(
            "Timeline", "dock_timeline", self.timeline_panel, Qt.DockWidgetArea.BottomDockWidgetArea
        )
        self.resizeDocks([self.media_dock, self.properties_dock], [280, 300], Qt.Orientation.Horizontal)
        self.resizeDocks([self.timeline_dock], [160], Qt.Orientation.Vertical)
        self._default_state = self.saveState()

        self._create_actions()
        self._create_menus()
        self._connect()
        self._restore_layout()
        self._update_title()

        if self.ffmpeg is None:
            self.statusBar().showMessage("FFmpeg not found: run tools\\setup_ffmpeg.py, then restart.")
        else:
            self.statusBar().showMessage(f"Ready · FFmpeg: {self.ffmpeg.ffmpeg}")

    # -------------------------------------------------------------- setup
    def _add_dock(self, title: str, name: str, widget: QWidget, area: Qt.DockWidgetArea) -> QDockWidget:
        dock = QDockWidget(title, self)
        dock.setObjectName(name)
        dock.setWidget(widget)
        dock.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
            | QDockWidget.DockWidgetFeature.DockWidgetClosable
        )
        self.addDockWidget(area, dock)
        return dock

    def _create_actions(self) -> None:
        self.act_import = QAction("&Import Video…", self, shortcut=QKeySequence("Ctrl+I"))
        self.act_new = QAction("&New Project", self, shortcut=QKeySequence.StandardKey.New)
        self.act_open = QAction("&Open Project…", self, shortcut=QKeySequence.StandardKey.Open)
        self.act_save = QAction("&Save Project", self, shortcut=QKeySequence.StandardKey.Save)
        self.act_save_as = QAction("Save Project &As…", self, shortcut=QKeySequence("Ctrl+Shift+S"))
        self.act_exit = QAction("E&xit", self, shortcut=QKeySequence("Alt+F4"))
        self.act_play = QAction("Play / Pause", self, shortcut=QKeySequence(Qt.Key.Key_Space))
        self.act_prev = QAction("Previous Frame", self, shortcut=QKeySequence(Qt.Key.Key_Left))
        self.act_next = QAction("Next Frame", self, shortcut=QKeySequence(Qt.Key.Key_Right))
        self.act_reset_layout = QAction("Reset Layout", self)
        self.act_about = QAction("About", self)
        self.act_ffmpeg_info = QAction("FFmpeg Info", self)
        for action in (self.act_play, self.act_prev, self.act_next):
            action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
            self.addAction(action)

    def _create_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        file_menu.addActions([self.act_new, self.act_open, self.act_save, self.act_save_as])
        file_menu.addSeparator()
        file_menu.addAction(self.act_import)
        self.recent_menu = file_menu.addMenu("Recent Files")
        file_menu.addSeparator()
        file_menu.addAction(self.act_exit)

        playback = self.menuBar().addMenu("&Playback")
        playback.addActions([self.act_play, self.act_prev, self.act_next])

        view = self.menuBar().addMenu("&View")
        for dock in (self.media_dock, self.properties_dock, self.timeline_dock):
            view.addAction(dock.toggleViewAction())
        view.addSeparator()
        view.addAction(self.act_reset_layout)

        help_menu = self.menuBar().addMenu("&Help")
        help_menu.addActions([self.act_ffmpeg_info, self.act_about])
        self._rebuild_recent_menu()

    def _connect(self) -> None:
        self.act_import.triggered.connect(self.choose_files)
        self.act_new.triggered.connect(self.new_project)
        self.act_open.triggered.connect(self.choose_project)
        self.act_save.triggered.connect(self.save)
        self.act_save_as.triggered.connect(self.save_as)
        self.act_exit.triggered.connect(self.close)
        self.act_play.triggered.connect(self.preview.toggle_play)
        self.act_prev.triggered.connect(lambda: self.preview.step_frames(-1))
        self.act_next.triggered.connect(lambda: self.preview.step_frames(1))
        self.act_reset_layout.triggered.connect(self.reset_layout)
        self.act_about.triggered.connect(self._show_about)
        self.act_ffmpeg_info.triggered.connect(self._show_ffmpeg_info)

        self.media_panel.import_requested.connect(self.choose_files)
        self.media_panel.media_selected.connect(self._on_media_selected)
        self.media_panel.remove_requested.connect(self._on_remove_media)
        self.preview.position_changed.connect(self.timeline_panel.set_position)
        self.preview.player.durationChanged.connect(self.timeline_panel.set_duration)
        self.timeline_panel.seek_requested.connect(self.preview.seek)
        self.media_imported.connect(self._on_media_imported)
        self.import_failed.connect(self._on_import_failed)

    # ------------------------------------------------------------- import
    def choose_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self, "Import Video", self.config.last_import_dir, _VIDEO_FILTER
        )
        if files:
            self.config.last_import_dir = str(Path(files[0]).parent)
            self.import_files(files)

    def import_files(self, files: list[str]) -> None:
        """Probe each file in a background thread; results arrive via signals."""
        if self.ffmpeg is None:
            QMessageBox.warning(
                self,
                "FFmpeg not found",
                "FFmpeg is required to import video.\n\n"
                "Run 'python tools\\setup_ffmpeg.py' in the application folder "
                "(or install FFmpeg and add it to PATH), then restart.",
            )
            return
        for file in files:
            path = Path(file)
            self.statusBar().showMessage(f"Reading {path.name}…")
            worker = Worker(probe_media, path, self.ffmpeg.ffprobe)
            worker.signals.finished.connect(lambda info, w=worker: self._worker_done(w, info, None))
            worker.signals.failed.connect(lambda exc, w=worker, p=path: self._worker_done(w, None, (p, exc)))
            self._pending.add(worker)
            self.pool.start(worker)

    def _worker_done(self, worker: Worker, info: MediaInfo | None, error) -> None:
        self._pending.discard(worker)
        if info is not None:
            self.media_imported.emit(info)
        else:
            path, exc = error
            message = str(exc) if isinstance(exc, MediaError) else f"Unexpected error: {exc}"
            self.import_failed.emit(str(path), message)

    def _on_media_imported(self, info: MediaInfo) -> None:
        log.info("Imported %s (%.2fs)", info.path, info.duration_s)
        added = self.project.add_media(
            MediaRef(path=str(info.path), size_bytes=info.size_bytes, duration_s=info.duration_s)
        )
        if added:
            self._set_dirty(True)
        self.media_panel.add_media(info)
        self.config.add_recent(str(info.path))
        self._rebuild_recent_menu()
        warnings = []
        if info.video is None:
            warnings.append("no video stream")
        if not info.audio:
            warnings.append("no audio stream")
        if info.video and info.video.is_vfr_suspected:
            warnings.append("variable frame rate")
        suffix = f" ({', '.join(warnings)})" if warnings else ""
        self.statusBar().showMessage(f"Imported {info.path.name}{suffix}", 6000)

    def _on_import_failed(self, path: str, message: str) -> None:
        log.warning("Import failed for %s: %s", path, message)
        self.statusBar().showMessage(f"Import failed: {Path(path).name}", 6000)
        QMessageBox.warning(self, "Import failed", f"{Path(path).name}\n\n{message}")

    def _on_media_selected(self, info: MediaInfo | None) -> None:
        self.properties_panel.show_media(info)
        if info is None:
            self.preview.unload()
            self.timeline_panel.set_duration(0)
        elif self.preview.media is None or self.preview.media.path != info.path:
            self.preview.load(info)
            self.timeline_panel.set_duration(int(info.duration_s * 1000))

    def _on_remove_media(self, info: MediaInfo) -> None:
        self.project.remove_media(str(info.path))
        self.media_panel.remove_media(info.path)
        self._set_dirty(True)

    # ------------------------------------------------------------ project
    def new_project(self) -> None:
        if not self._confirm_discard():
            return
        self.project = Project()
        self.project_path = None
        self.media_panel.clear()
        self.preview.unload()
        self.properties_panel.show_media(None)
        self._set_dirty(False)

    def choose_project(self) -> None:
        if not self._confirm_discard():
            return
        file, _ = QFileDialog.getOpenFileName(
            self, "Open Project", "", f"Project (*{PROJECT_EXTENSION})"
        )
        if file:
            self.open_project(Path(file))

    def open_project(self, path: Path) -> None:
        try:
            project = load_project(path)
        except (OSError, ProjectFormatError) as exc:
            QMessageBox.warning(self, "Cannot open project", f"{path.name}\n\n{exc}")
            return
        self.project = project
        self.project_path = path
        self.media_panel.clear()
        self.preview.unload()
        missing = [m.path for m in project.media if not Path(m.path).is_file()]
        self.import_files([m.path for m in project.media if Path(m.path).is_file()])
        self._set_dirty(False)
        if missing:
            QMessageBox.warning(
                self, "Missing media", "These source files were not found:\n\n" + "\n".join(missing)
            )

    def save(self) -> bool:
        if self.project_path is None:
            return self.save_as()
        try:
            self.project_path = save_project(self.project, self.project_path)
        except OSError as exc:
            QMessageBox.warning(self, "Save failed", str(exc))
            return False
        self._set_dirty(False)
        self.statusBar().showMessage(f"Saved {self.project_path}", 4000)
        return True

    def save_as(self) -> bool:
        file, _ = QFileDialog.getSaveFileName(
            self, "Save Project", f"{self.project.name}{PROJECT_EXTENSION}", f"Project (*{PROJECT_EXTENSION})"
        )
        if not file:
            return False
        self.project_path = Path(file)
        self.project.name = self.project_path.stem
        return self.save()

    def _confirm_discard(self) -> bool:
        if not self._dirty:
            return True
        answer = QMessageBox.question(
            self,
            "Unsaved changes",
            "Save changes to the current project?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Save:
            return self.save()
        return answer == QMessageBox.StandardButton.Discard

    def _set_dirty(self, dirty: bool) -> None:
        self._dirty = dirty
        self._update_title()

    def _update_title(self) -> None:
        name = self.project_path.name if self.project_path else "Untitled"
        self.setWindowTitle(f"{name}{' *' if self._dirty else ''} — {APP_NAME}")

    # ------------------------------------------------------------- layout
    def reset_layout(self) -> None:
        for dock in (self.media_dock, self.properties_dock, self.timeline_dock):
            dock.setFloating(False)
            dock.show()
        self.restoreState(self._default_state)

    def _restore_layout(self) -> None:
        if self.config.window_geometry:
            self.restoreGeometry(QByteArray.fromBase64(self.config.window_geometry.encode()))
        if self.config.window_state:
            self.restoreState(QByteArray.fromBase64(self.config.window_state.encode()))

    def _rebuild_recent_menu(self) -> None:
        self.recent_menu.clear()
        for file in self.config.recent_files:
            action = self.recent_menu.addAction(file)
            action.triggered.connect(lambda _=False, f=file: self.import_files([f]))
        self.recent_menu.setEnabled(bool(self.config.recent_files))

    # ------------------------------------------------------------- events
    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        files = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        projects = [f for f in files if f.lower().endswith(PROJECT_EXTENSION)]
        videos = [f for f in files if Path(f).suffix.lower() in SUPPORTED_EXTENSIONS]
        if projects and self._confirm_discard():
            self.open_project(Path(projects[0]))
        elif videos:
            self.import_files(videos)

    def closeEvent(self, event) -> None:  # noqa: N802
        if not self._confirm_discard():
            event.ignore()
            return
        self.preview.player.stop()
        self.config.window_geometry = bytes(self.saveGeometry().toBase64()).decode()
        self.config.window_state = bytes(self.saveState().toBase64()).decode()
        self.pool.waitForDone(3000)
        event.accept()

    # --------------------------------------------------------------- help
    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            f"About {APP_NAME}",
            f"<b>{APP_NAME}</b> {APP_VERSION}<br>"
            "Vertical Shorts editor for Twitch clips.<br>Phase 1: import and preview.",
        )

    def _show_ffmpeg_info(self) -> None:
        if self.ffmpeg is None:
            text = "FFmpeg was not found."
        else:
            text = (
                f"ffmpeg: {self.ffmpeg.ffmpeg}\n{tool_version(self.ffmpeg.ffmpeg)}\n\n"
                f"ffprobe: {self.ffmpeg.ffprobe}\n{tool_version(self.ffmpeg.ffprobe)}"
            )
        QMessageBox.information(self, "FFmpeg", text)
