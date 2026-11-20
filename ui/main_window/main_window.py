"""Main window: docks, menus, import flow and project save/open."""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QByteArray, QSize, QThreadPool, Qt, Signal
from PySide6.QtGui import QAction, QActionGroup, QDragEnterEvent, QDropEvent, QKeySequence
from PySide6.QtWidgets import (
    QDockWidget,
    QFileDialog,
    QFrame,
    QLabel,
    QMainWindow,
    QMessageBox,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from app.config import APP_NAME, APP_VERSION, AppConfig
from core.audio.waveform import load_waveform
from core.censor.dictionary import Dictionary
from core.censor.review import detect
from core.render.encoders import detect_encoders
from core.render.settings import RenderSettings
from core.metadata.library import Library
from core.youtube.history import UploadHistory
from core.youtube.service import YouTubeService
from core.editor.composition import default_composition
from core.editor.presets import PresetStore
from core.history import History
from core.timeline.timebase import format_timecode
from core.media.errors import MediaError
from core.media.ffmpeg_locator import FFmpegPaths, tool_version
from core.media.probe import SUPPORTED_EXTENSIONS, MediaInfo, probe_media
from core.media.thumbnail import make_thumbnail
from core.subtitles.export import write_subtitles
from core.subtitles.model import SubtitleSettings, build_captions
from core.projects.project import (
    PROJECT_EXTENSION,
    MediaRef,
    Project,
    ProjectFormatError,
    load_project,
    save_project,
)
from core.timeline.model import Timeline
from core.transcription.engine import (
    TranscriptionError,
    WhisperEngine,
    downloaded_models,
    learned_words,
    merge_learned,
    transcribe_file,
    transcribe_range,
)
from core.timeline.timebase import ms_to_us, seconds_to_us, us_to_ms
from core.workers import CancellableTask, Worker
from ui.censor.censor_controller import CensorController
from ui.censor.censor_panel import CensorPanel
from ui.censor.dictionary_dialog import DictionaryDialog
from ui.editor.composition_controller import CompositionController
from ui.export.export_dialog import ExportDialog, default_output_path
from ui.metadata.library_dialog import LibraryDialog
from ui.metadata.metadata_panel import MetadataPanel
from ui.subtitles.subtitles_panel import SubtitlesPanel
from ui.theme import icon, palette, windows
from ui.twitch.import_dialog import TwitchImportDialog
from core.twitch.autofill import autofill
from core.twitch.clip import parse_clip_url
from ui.youtube.accounts_dialog import YouTubeAccountsDialog
from ui.youtube.upload_dialog import UploadDialog
from ui.panels.composition_panel import CompositionPanel
from ui.panels.media_panel import MediaPanel
from ui.panels.properties_panel import PropertiesPanel
from ui.panels.timeline_panel import TimelinePanel
from ui.preview.edit_playback import EditPlayback
from ui.preview.preview_panel import PreviewPanel
from ui.highlights.highlights_panel import HighlightsPanel
from ui.main_window.autoframe_runner import AutoframeRunner
from ui.main_window.highlights_controller import HighlightsController
from ui.main_window.listen_preview import ListenPreview
from ui.main_window.pages import DATA, EDIT, PAGES, PUBLISH, RENDER, PageBar, StepPage, page_scroll
from ui.main_window.session_keeper import AUTOSAVE_CHOICES, SessionKeeper
from ui.timeline.timeline_controller import TimelineController
from ui.transcript.transcript_controller import TranscriptController
from ui.transcript.transcript_panel import TranscriptPanel

log = logging.getLogger(__name__)

LAYOUT_VERSION = 10  # bump when the dock arrangement changes

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
        self.restart_requested = False
        windows.prepare_mica(self)

        self.composition = CompositionController(self)
        self.presets = PresetStore(config.data_dir / "presets.json")
        self.timeline = TimelineController(self)
        self._loading_composition = False  # True while state is pushed in, not edited
        self._main_layout = self.composition.composition  # the project's layout (parts without their own use it)
        self.transcript = TranscriptController(self)
        self.engine = WhisperEngine(config.data_dir / "models")
        self._transcribe_task: CancellableTask | None = None
        self._transcribe_path: Path | None = None
        self.censor = CensorController(self)
        self.censor_dictionary_path = config.data_dir / "censor_dictionary.json"
        self.censor_dictionary = Dictionary.load(self.censor_dictionary_path)
        self._preview_stop_us: int | None = None
        self.encoders = None  # usable video encoders, detected in the background
        self.youtube = YouTubeService(config)
        self.upload_history = UploadHistory(config.data_dir / "youtube_uploads.json")  # pause here when listening to a suggestion
        self.metadata_library = Library(config.data_dir / "metadata_library.sqlite")
        self.history: History = History(self._edit_state())
        self.session = SessionKeeper(self)  # autosave + undo history kept between runs

        self.preview = PreviewPanel(self.composition, self)
        # Steps as pages (Resolve-style): the editor, then data, render and publish pages.
        self.page_stack = QStackedWidget(self)
        self.page_stack.addWidget(self.preview)
        self.setCentralWidget(self.page_stack)
        self._page = EDIT
        self._edit_layout: QByteArray | None = None  # the Edit page's docks while another page is shown
        self._rendered: tuple | None = None  # (path, edit state) of the last export
        self._published: str = ""  # path of the last uploaded export

        self.media_panel = MediaPanel(self)
        self.properties_panel = PropertiesPanel(self)  # media info
        self.composition_panel = CompositionPanel(self.composition, self.presets, self)
        self.timeline_panel = TimelinePanel(self.timeline, self)
        self.edit_playback = EditPlayback(self.preview, self.timeline, self)
        self.listen = ListenPreview(self)  # Timeline → Listen (noise reduction + loudness)
        self.autoframe = AutoframeRunner(self)  # Composition → Face → Auto-frame
        self.transcript_panel = TranscriptPanel(self.transcript, self)
        self.censor_panel = CensorPanel(self.censor, self)
        self.metadata_panel = MetadataPanel(self.metadata_library, self)
        self.subtitles_panel = SubtitlesPanel(self)
        self.highlights_panel = HighlightsPanel(self)
        self.captions: list = []  # current captions (output time)
        data = QWidget(self)
        data_column = QVBoxLayout(data)
        data_column.setContentsMargins(0, 0, 0, 0)
        data_heading = QLabel("Data", data)
        data_heading.setObjectName("pageTitle")
        data_column.addWidget(data_heading)
        data_column.addWidget(self.metadata_panel)
        self.data_page = page_scroll(data, self)
        self.page_stack.addWidget(self.data_page)
        self.render_page = StepPage(
            "Render", "Export the edited Short to a video file: format, quality, loudness, subtitles.",
            "Export video…", "export", self)
        self.page_stack.addWidget(page_scroll(self.render_page, self, 720))
        self.publish_page = StepPage(
            "Publish", "Upload the exported video to YouTube with the title, description and tags from Data.",
            "Upload to YouTube…", "youtube", self)
        self.page_stack.addWidget(page_scroll(self.publish_page, self, 720))

        composition_scroll = QScrollArea(self)
        composition_scroll.setWidgetResizable(True)
        composition_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        composition_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        composition_scroll.setWidget(self.composition_panel)
        self.properties_tabs = QTabWidget(self)
        self.properties_tabs.setDocumentMode(True)
        self.properties_tabs.addTab(composition_scroll, "Composition")
        self.properties_tabs.addTab(self.properties_panel, "Media")

        self.media_dock = self._add_dock("Media", "dock_media", self.media_panel, Qt.DockWidgetArea.LeftDockWidgetArea)
        self.properties_dock = self._add_dock(
            "Properties", "dock_properties", self.properties_tabs, Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.timeline_dock = self._add_dock(
            "Timeline", "dock_timeline", self.timeline_panel, Qt.DockWidgetArea.BottomDockWidgetArea
        )
        self.transcript_dock = self._add_dock(
            "Transcript", "dock_transcript", self.transcript_panel, Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.censor_dock = self._add_dock(
            "Censor", "dock_censor", self.censor_panel, Qt.DockWidgetArea.RightDockWidgetArea
        )
        subtitles_scroll = QScrollArea(self)
        subtitles_scroll.setWidgetResizable(True)
        subtitles_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        subtitles_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        subtitles_scroll.setWidget(self.subtitles_panel)
        self.subtitles_dock = self._add_dock(
            "Subtitles", "dock_subtitles", subtitles_scroll, Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.tabifyDockWidget(self.properties_dock, self.transcript_dock)
        self.tabifyDockWidget(self.transcript_dock, self.subtitles_dock)
        self.tabifyDockWidget(self.subtitles_dock, self.censor_dock)
        self.highlights_dock = self._add_dock(
            "Highlights", "dock_highlights", self.highlights_panel, Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.tabifyDockWidget(self.transcript_dock, self.highlights_dock)
        self.properties_dock.raise_()
        self.resizeDocks([self.media_dock, self.properties_dock], [240, 360], Qt.Orientation.Horizontal)
        self.resizeDocks([self.timeline_dock], [250], Qt.Orientation.Vertical)
        self._default_state = self.saveState(LAYOUT_VERSION)

        self.setTabPosition(Qt.DockWidgetArea.AllDockWidgetAreas, QTabWidget.TabPosition.North)
        self._create_actions()
        self._create_menus()
        self._create_toolbar()
        self._create_page_bar()
        self._connect()
        self.highlights = HighlightsController(self)  # Highlights dock ↔ timeline markers
        self._restore_layout()
        self._update_dock_titles()
        self.timeline_panel.view.restore_view_state(self.config.timeline_view)
        self._update_title()
        self._refresh_steps()

        if self.ffmpeg is not None:
            self._start_encoder_detection()
        if self.ffmpeg is None:
            self.statusBar().showMessage("FFmpeg not found: run tools\\setup_ffmpeg.py, then restart.")
        else:
            self.statusBar().showMessage(f"Ready · FFmpeg: {self.ffmpeg.ffmpeg}")

    # -------------------------------------------------------------- setup
    def _create_toolbar(self) -> None:
        """Top bar: app name, project, file and history actions, Export and Upload."""
        bar = QToolBar("Main toolbar", self)
        bar.setObjectName("mainToolbar")
        bar.setMovable(False)
        bar.setFloatable(False)
        bar.toggleViewAction().setEnabled(False)
        bar.setIconSize(QSize(18, 18))
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        bar.setContextMenuPolicy(Qt.ContextMenuPolicy.PreventContextMenu)
        title = QLabel(APP_NAME, bar)
        title.setObjectName("appTitle")
        bar.addWidget(title)
        self.project_label = QLabel(bar)
        self.project_label.setObjectName("projectName")
        bar.addWidget(self.project_label)

        def separator() -> None:
            line = QFrame(bar)
            line.setObjectName("toolbarSeparator")
            line.setFixedHeight(22)
            bar.addWidget(line)

        separator()
        for action, name in ((self.act_import, "import"), (self.act_twitch, "link"), (self.act_open, "folder"),
                             (self.act_save, "save")):
            action.setIcon(icon(name))
            bar.addAction(action)
        separator()
        for action, name in ((self.act_undo, "undo"), (self.act_redo, "redo")):
            action.setIcon(icon(name))
            bar.addAction(action)
            bar.widgetForAction(action).setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        spacer = QWidget(bar)
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        spacer.setStyleSheet("background: transparent;")
        bar.addWidget(spacer)
        self.act_upload.setIcon(icon("youtube"))
        bar.addAction(self.act_upload)
        bar.widgetForAction(self.act_upload).setObjectName("youtubeAction")
        self.act_export.setIcon(icon("export", palette.accent().on_fill))
        bar.addAction(self.act_export)
        bar.widgetForAction(self.act_export).setObjectName("primaryAction")
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, bar)
        self.main_toolbar = bar
        self.act_import.setIconText("Import")
        self.act_twitch.setIconText("Twitch link")
        self.act_open.setIconText("Open")
        self.act_save.setIconText("Save")
        self.act_export.setIconText("Export")
        self.act_upload.setIconText("Upload")

    def _create_page_bar(self) -> None:
        """Bottom bar with the work steps, like the page buttons in DaVinci Resolve."""
        bar = QToolBar("Steps", self)
        bar.setObjectName("pageToolbar")
        bar.setMovable(False)
        bar.setFloatable(False)
        bar.toggleViewAction().setEnabled(False)
        bar.setContextMenuPolicy(Qt.ContextMenuPolicy.PreventContextMenu)
        self.page_bar = PageBar(bar)
        self.page_bar.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        bar.addWidget(self.page_bar)
        self.addToolBar(Qt.ToolBarArea.BottomToolBarArea, bar)
        self.page_toolbar = bar
        self.page_bar.page_requested.connect(self.set_page)
        self.page_actions: list[QAction] = []
        for index, (title, _icon, _hint) in enumerate(PAGES):
            action = QAction(f"{index + 1}  {title}", self)
            action.setShortcut(QKeySequence(f"Ctrl+{index + 1}"))
            action.triggered.connect(lambda _=False, i=index: self.set_page(i))
            self.addAction(action)
            self.page_actions.append(action)
        self.render_page.action_requested.connect(self.act_export.trigger)
        self.publish_page.action_requested.connect(self._publish_latest)

    # --------------------------------------------------------------- pages
    @property
    def page(self) -> int:
        return self._page

    def set_page(self, index: int) -> None:
        """Show a step. The Edit page keeps its docks; other pages fill the window."""
        index = max(0, min(index, len(PAGES) - 1))
        if index != self._page:
            if self._page == EDIT:
                if self.preview.is_playing:
                    self.preview.pause()
                self._edit_layout = self.saveState(LAYOUT_VERSION)
                for dock in self._docks():
                    dock.hide()
            self.page_stack.setCurrentIndex(index)
            if index == EDIT and self._edit_layout is not None:
                self.restoreState(self._edit_layout, LAYOUT_VERSION)
                self._edit_layout = None
                self._update_dock_titles()
            self._page = index
        self.page_bar.set_current(index)
        self._refresh_steps()

    def _edit_layout_state(self) -> QByteArray:
        return self._edit_layout if self._edit_layout is not None else self.saveState(LAYOUT_VERSION)

    def _render_state(self) -> tuple:
        return (*self._edit_state(), self.project.subtitles)

    def _rendered_path(self) -> Path | None:
        if self._rendered is None:
            return None
        path, state = self._rendered
        return path if state == self._render_state() and Path(path).is_file() else None

    def _refresh_steps(self) -> None:
        """Done marks on the step bar and the texts of the Render and Publish pages."""
        if not hasattr(self, "page_bar"):
            return
        timeline = self.timeline.timeline
        metadata = self.project.metadata
        rendered = self._rendered_path()
        self.page_bar.set_done(EDIT, timeline is not None)
        self.page_bar.set_done(DATA, bool(metadata.title.strip()))
        self.page_bar.set_done(RENDER, rendered is not None)
        self.page_bar.set_done(PUBLISH, rendered is not None and self._published == str(rendered))
        if timeline is None:
            self.render_page.set_status("Import a clip and edit it on the Edit page first.", False)
        else:
            text = f"Length of the Short: <b>{format_timecode(timeline.output_us)}</b>"
            if rendered is not None:
                text += f"<br>Exported: {rendered}"
            elif self._rendered is not None:
                text += "<br>The edit changed since the last export: export again."
            self.render_page.set_status(text, self.ffmpeg is not None and self.preview.media is not None)
        title = metadata.title.strip() or "<i>no title yet: fill it in on the Data page</i>"
        if rendered is None:
            self.publish_page.set_status(f"Title: {title}<br>Export the video on the Render page first, "
                                         "or pick a file to upload.", True)
        else:
            done = "<br><b>Uploaded.</b>" if self._published == str(rendered) else ""
            self.publish_page.set_status(f"Title: {title}<br>File: {rendered}{done}", True)

    def _publish_latest(self) -> None:
        self.upload_to_youtube(self._rendered_path())

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
        dock.dockLocationChanged.connect(lambda _area: self._update_dock_titles())
        dock.topLevelChanged.connect(lambda _floating: self._update_dock_titles())
        return dock

    def _docks(self) -> list[QDockWidget]:
        return self.findChildren(QDockWidget)

    def _update_dock_titles(self) -> None:
        """Hide a dock's own title bar while it sits in a tab group: the tab
        already names it, so the title would only repeat the same word."""
        for dock in self._docks():
            tabbed = not dock.isFloating() and bool(self.tabifiedDockWidgets(dock))
            has_blank = dock.titleBarWidget() is not None
            if tabbed and not has_blank:
                blank = QWidget(dock)
                blank.setObjectName("dockTitleHidden")
                blank.setFixedHeight(0)
                dock.setTitleBarWidget(blank)
            elif not tabbed and has_blank:
                old = dock.titleBarWidget()
                dock.setTitleBarWidget(None)
                old.deleteLater()

    def _create_actions(self) -> None:
        self.act_import = QAction("&Import Video…", self, shortcut=QKeySequence("Ctrl+I"))
        self.act_twitch = QAction("Import from &Twitch…", self, shortcut=QKeySequence("Ctrl+Shift+I"))
        self.act_twitch.setToolTip("Download a Twitch clip by its link and fill in the metadata (Ctrl+Shift+I)")
        self.act_export = QAction("&Export Video…", self, shortcut=QKeySequence("Ctrl+E"))
        self.act_export.setToolTip("Render the Short to MP4 / MKV (Ctrl+E)")
        self.act_upload = QAction("&Upload to YouTube…", self, shortcut=QKeySequence("Ctrl+U"))
        self.act_upload.setToolTip("Upload a rendered video to YouTube (Ctrl+U)")
        self.act_youtube_accounts = QAction("YouTube Accounts…", self)
        self.act_metadata_library = QAction("Metadata Library…", self)
        self.act_metadata_library.setToolTip("Streamers, participants, games, stream types and templates")
        self.act_new = QAction("&New Project", self, shortcut=QKeySequence.StandardKey.New)
        self.act_open = QAction("&Open Project…", self, shortcut=QKeySequence.StandardKey.Open)
        self.act_save = QAction("&Save Project", self, shortcut=QKeySequence.StandardKey.Save)
        self.act_save_as = QAction("Save Project &As…", self, shortcut=QKeySequence("Ctrl+Shift+S"))
        self.act_exit = QAction("E&xit", self, shortcut=QKeySequence("Alt+F4"))
        self.act_recover = QAction("Restore Unsaved Work…", self)
        self.act_recover.setToolTip("Open a recovery copy left when the app was closed without saving")
        self.act_play = QAction("Play / Pause", self, shortcut=QKeySequence(Qt.Key.Key_Space))
        self.act_prev = QAction("Previous Frame", self, shortcut=QKeySequence(Qt.Key.Key_Left))
        self.act_next = QAction("Next Frame", self, shortcut=QKeySequence(Qt.Key.Key_Right))
        self.act_back_second = QAction("Back 1 Second", self, shortcut=QKeySequence("Shift+Left"))
        self.act_forward_second = QAction("Forward 1 Second", self, shortcut=QKeySequence("Shift+Right"))
        self.act_go_start = QAction("Go to Start", self, shortcut=QKeySequence(Qt.Key.Key_Home))
        self.act_go_end = QAction("Go to End", self, shortcut=QKeySequence(Qt.Key.Key_End))
        self.act_zoom_in = QAction("Zoom In", self)
        self.act_zoom_in.setShortcuts([QKeySequence("+"), QKeySequence("="), QKeySequence("Ctrl+=")])
        self.act_zoom_out = QAction("Zoom Out", self)
        self.act_zoom_out.setShortcuts([QKeySequence("-"), QKeySequence("Ctrl+-")])
        self.act_zoom_fit = QAction("Fit Whole Clip", self, shortcut=QKeySequence("0"))
        self.act_zoom_selection = QAction("Zoom to Selection", self, shortcut=QKeySequence(Qt.Key.Key_Z))
        self.act_reset_layout = QAction("Reset Layout", self)
        self.act_about = QAction("About", self)
        self.act_ffmpeg_info = QAction("FFmpeg Info", self)
        self.act_undo = QAction("Undo", self, shortcut=QKeySequence.StandardKey.Undo)
        self.act_redo = QAction("Redo", self)
        self.act_redo.setShortcuts([QKeySequence("Ctrl+Y"), QKeySequence("Ctrl+Shift+Z")])
        self.act_split = QAction("Split", self, shortcut=QKeySequence(Qt.Key.Key_S))
        self.act_split.setToolTip("Split the clip at the playhead (S)")
        self.act_delete = QAction("Delete", self, shortcut=QKeySequence(Qt.Key.Key_Delete))
        self.act_delete.setToolTip("Delete the selected mute region, range or segment (Del)")
        self.act_trim = QAction("Trim to selection", self, shortcut=QKeySequence(Qt.Key.Key_T))
        self.act_trim.setToolTip("Keep only the selected range (T)")
        self.act_restore = QAction("Restore all", self)
        self.act_restore.setToolTip("Bring back everything that was cut")
        self.act_mute = QAction("Mute selection", self, shortcut=QKeySequence(Qt.Key.Key_M))
        self.act_mute.setToolTip("Create a Mute Region from the selected range (M)")
        self.act_merge_mutes = QAction("Merge overlapping mutes", self)
        self._edit_actions = (
            self.act_undo, self.act_redo, self.act_split, self.act_delete, self.act_trim,
            self.act_restore, self.act_mute, self.act_merge_mutes,
        )
        self._navigation_actions = (
            self.act_play, self.act_prev, self.act_next, self.act_back_second, self.act_forward_second,
            self.act_go_start, self.act_go_end, self.act_zoom_in, self.act_zoom_out, self.act_zoom_fit,
            self.act_zoom_selection,
        )
        for action in (*self._navigation_actions, *self._edit_actions):
            action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
            self.addAction(action)

    def _create_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        file_menu.addActions([self.act_new, self.act_open, self.act_save, self.act_save_as])
        file_menu.addSeparator()
        file_menu.addAction(self.act_import)
        file_menu.addAction(self.act_twitch)
        file_menu.addAction(self.act_export)
        file_menu.addAction(self.act_upload)
        self.recent_menu = file_menu.addMenu("Recent Files")
        file_menu.addAction(self.act_recover)
        file_menu.addSeparator()
        file_menu.addAction(self.act_exit)

        edit = self.menuBar().addMenu("&Edit")
        edit.addActions([self.act_undo, self.act_redo])
        edit.addSeparator()
        edit.addActions([self.act_split, self.act_delete, self.act_trim, self.act_restore])
        edit.addSeparator()
        edit.addActions([self.act_mute, self.act_merge_mutes])
        for action, glyph, short in ((self.act_split, "split", "Split"), (self.act_delete, "remove", "Delete"),
                                     (self.act_trim, "trim", "Trim"), (self.act_mute, "volume_off", "Mute")):
            action.setIcon(icon(glyph))
            action.setIconText(short)
        # Undo / redo live in the main command bar only.
        self.timeline_panel.add_actions([self.act_split, self.act_delete, self.act_trim, None, self.act_mute])

        playback = self.menuBar().addMenu("&Playback")
        playback.addActions([self.act_play, self.act_prev, self.act_next])
        playback.addSeparator()
        playback.addActions([self.act_back_second, self.act_forward_second, self.act_go_start, self.act_go_end])

        view = self.menuBar().addMenu("&View")
        for dock in (self.media_dock, self.properties_dock, self.transcript_dock, self.highlights_dock, self.subtitles_dock,
                     self.censor_dock, self.timeline_dock):
            view.addAction(dock.toggleViewAction())
        view.addSeparator()
        zoom = view.addMenu("Timeline Zoom")
        zoom.addActions([self.act_zoom_in, self.act_zoom_out, self.act_zoom_fit, self.act_zoom_selection])
        view.addSeparator()
        view.addAction(self.act_reset_layout)

        settings_menu = self.menuBar().addMenu("&Settings")
        appearance = settings_menu.addMenu("Appearance")
        self._theme_group = QActionGroup(self)
        self.theme_actions: dict[str, QAction] = {}
        for name, text in ((palette.SYSTEM, "Match Windows"), (palette.DARK, "Dark"), (palette.LIGHT, "Light")):
            action = appearance.addAction(text)
            action.setCheckable(True)
            action.setChecked(self.config.theme == name)
            action.setData(name)
            self._theme_group.addAction(action)
            self.theme_actions[name] = action
        if not any(a.isChecked() for a in self.theme_actions.values()):
            self.theme_actions[palette.DARK].setChecked(True)
        self._theme_group.triggered.connect(lambda action: self.set_appearance(theme=action.data()))
        appearance.addSeparator()
        self.act_mica = appearance.addAction("Mica backdrop (Windows 11)")
        self.act_mica.setCheckable(True)
        self.act_mica.setChecked(self.config.mica)
        self.act_mica.setEnabled(windows.mica_supported())
        self.act_mica.setToolTip("Lets the desktop colour show through the top bar. Off by default.")
        self.act_mica.toggled.connect(lambda on: self.set_appearance(mica=on))
        autosave = settings_menu.addMenu("Autosave")
        self.act_autosave = autosave.addAction("Keep a recovery copy of unsaved work")
        self.act_autosave.setCheckable(True)
        self.act_autosave.setChecked(self.config.autosave_enabled)
        self.act_autosave.toggled.connect(self.set_autosave)
        autosave.addSeparator()
        self._autosave_group = QActionGroup(self)
        self.autosave_actions: dict[int, QAction] = {}
        for minutes in AUTOSAVE_CHOICES:
            action = autosave.addAction(f"Every {minutes} min")
            action.setCheckable(True)
            action.setChecked(self.config.autosave_minutes == minutes)
            action.setData(minutes)
            self._autosave_group.addAction(action)
            self.autosave_actions[minutes] = action
        self._autosave_group.triggered.connect(lambda action: self.set_autosave(minutes=action.data()))
        settings_menu.addSeparator()
        settings_menu.addAction(self.act_metadata_library)
        integrations = settings_menu.addMenu("Integrations")
        integrations.addAction(self.act_youtube_accounts)

        help_menu = self.menuBar().addMenu("&Help")
        help_menu.addActions([self.act_ffmpeg_info, self.act_about])
        self._rebuild_recent_menu()

    def _connect(self) -> None:
        self.timeline.changed.connect(self._refresh_steps)
        self.act_import.triggered.connect(self.choose_files)
        self.act_twitch.triggered.connect(lambda: self.import_from_twitch())
        self.act_export.triggered.connect(self.export_video)
        self.act_upload.triggered.connect(lambda: self.upload_to_youtube())
        self.act_youtube_accounts.triggered.connect(self.manage_youtube_accounts)
        self.act_metadata_library.triggered.connect(lambda: self.open_metadata_library())
        self.metadata_panel.metadata_changed.connect(self._metadata_changed)
        self.metadata_panel.library_requested.connect(self.open_metadata_library)
        self.act_new.triggered.connect(self.new_project)
        self.act_open.triggered.connect(self.choose_project)
        self.act_save.triggered.connect(self.save)
        self.act_save_as.triggered.connect(self.save_as)
        self.act_exit.triggered.connect(self.close)
        self.act_recover.triggered.connect(lambda: self.session.offer_recovery(quiet=False))
        self.act_play.triggered.connect(self.preview.toggle_play)
        self.act_prev.triggered.connect(lambda: self.preview.step_frames(-1))
        self.act_next.triggered.connect(lambda: self.preview.step_frames(1))
        self.act_back_second.triggered.connect(lambda: self.preview.step_ms(-1000))
        self.act_forward_second.triggered.connect(lambda: self.preview.step_ms(1000))
        self.act_go_start.triggered.connect(lambda: self.preview.seek(0))
        self.act_go_end.triggered.connect(self.preview.seek_end)
        timeline_view = self.timeline_panel.view
        self.act_zoom_in.triggered.connect(timeline_view.zoom_in)
        self.act_zoom_out.triggered.connect(timeline_view.zoom_out)
        self.act_zoom_fit.triggered.connect(timeline_view.zoom_fit)
        self.act_zoom_selection.triggered.connect(timeline_view.zoom_selection)
        self.act_reset_layout.triggered.connect(self.reset_layout)
        self.act_about.triggered.connect(self._show_about)
        self.act_ffmpeg_info.triggered.connect(self._show_ffmpeg_info)

        self.media_panel.import_requested.connect(self.choose_files)
        self.media_panel.media_selected.connect(self._on_media_selected)
        self.media_panel.remove_requested.connect(self._on_remove_media)
        self.act_undo.triggered.connect(self.undo)
        self.act_redo.triggered.connect(self.redo)
        self.act_split.triggered.connect(self.timeline.split_at_playhead)
        self.act_delete.triggered.connect(self.timeline.delete_selection)
        self.act_trim.triggered.connect(self.timeline.trim_to_range)
        self.act_restore.triggered.connect(self.timeline.restore_all)
        self.act_mute.triggered.connect(lambda: self.timeline.mute_range())
        self.act_merge_mutes.triggered.connect(self.timeline.merge_mutes)

        self.preview.position_changed.connect(lambda ms: self.timeline.set_playhead(ms_to_us(ms)))
        self.timeline_panel.view.seek_requested.connect(lambda us: self.preview.seek(us_to_ms(us)))
        self.timeline_panel.view.gesture_finished.connect(lambda: self.history.break_merge())
        self.composition.changed.connect(self._on_layout_changed)
        self.timeline.playhead_changed.connect(lambda _us: self._refresh_layout())
        self.timeline.playhead_changed.connect(self.composition.set_time)
        self.timeline.changed.connect(self._refresh_layout)
        self.composition_panel.apply_all_requested.connect(self.apply_layout_to_all_parts)
        self.timeline.changed.connect(lambda: self._on_edit_changed(self.timeline.last_change_key))
        self.transcript.changed.connect(lambda: self._on_edit_changed(self.transcript.last_change_key))
        self.transcript.changed.connect(
            lambda: self.timeline_panel.view.set_transcript(self.transcript.transcript)
        )
        self.transcript_panel.transcribe_requested.connect(self.start_transcription)
        self.transcript_panel.cancel_requested.connect(self.cancel_transcription)
        self.transcript_panel.clear_requested.connect(lambda: self.transcript.set_transcript(None))
        self.transcript_panel.seek_requested.connect(lambda us: self.preview.seek(us_to_ms(us)))
        self.transcript_panel.word_selected.connect(self.timeline.set_range)
        self.timeline.playhead_changed.connect(self.transcript_panel.follow_playhead)
        self.transcript_panel.set_downloaded_models(downloaded_models(self.engine.models_dir))
        self.transcript_panel.set_preferences(
            self.config.transcribe_model,
            self.config.transcribe_language,
            self.config.transcribe_vocabulary,
            self.config.transcribe_uncensored,
            self.config.transcribe_slang,
        )
        self.transcript_panel.set_quality_preferences(
            self.config.transcribe_clean_audio,
            self.config.transcribe_vad,
            self.config.transcribe_accurate,
            self.config.transcribe_custom_model,
            self.config.transcribe_separate_voice,
            self.config.transcribe_refine_timing,
        )
        self.transcript_panel.set_learned(self.config.transcribe_learned)
        self.transcript.corrected.connect(self.learn_correction)
        self.transcript_panel.learned_cleared.connect(self.forget_learned)
        self.transcript_panel.retranscribe_requested.connect(self.retranscribe_range)
        self.transcript_panel.speakers_requested.connect(self.find_speakers)
        self.transcript_panel.rules_requested.connect(self.edit_replacement_rules)
        self.timeline.selection_changed.connect(self._update_retranscribe)
        self.timeline.playhead_changed.connect(lambda us: self._update_retranscribe())
        self.transcript.changed.connect(self._update_retranscribe)
        self.censor.changed.connect(lambda: self._on_edit_changed(self.censor.last_change_key))
        self.censor.changed.connect(self._refresh_censor_view)
        self.censor.current_changed.connect(self._on_censor_current)
        self.timeline.changed.connect(self._refresh_censor_view)
        self.transcript.changed.connect(
            lambda: self.censor_panel.set_has_transcript(self.transcript.transcript is not None)
        )
        self.subtitles_panel.settings_changed.connect(self._subtitles_changed)
        self.subtitles_panel.seek_requested.connect(self._seek_output)
        self.subtitles_panel.split_requested.connect(self.split_caption_at_playhead)
        self.subtitles_panel.export_requested.connect(self.save_subtitles)
        self.transcript.changed.connect(self._refresh_subtitles)
        self.timeline.changed.connect(self._refresh_subtitles)
        self.composition.changed.connect(self.preview.output_view.update)
        self.timeline.playhead_changed.connect(self._update_subtitle_time)
        self.censor_panel.detect_requested.connect(self.detect_censor)
        self.censor_panel.apply_requested.connect(self.apply_censor)
        self.censor_panel.remove_requested.connect(self.remove_censor_mutes)
        self.censor_panel.exclude_requested.connect(self.exclude_censor_word)
        self.censor_panel.word_edited.connect(self.edit_censor_word)
        self.censor_panel.preview_requested.connect(self.preview_range)
        self.censor_panel.dictionary_requested.connect(self.edit_censor_dictionary)
        self.censor_panel.range_edit_finished.connect(lambda: self.history.break_merge())
        self.timeline.playhead_changed.connect(self._check_preview_stop)
        self.timeline.selection_changed.connect(self._update_edit_actions)
        self._update_edit_actions()
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

    def _make_thumbnail(self, info: MediaInfo) -> None:
        if self.ffmpeg is None or info.video is None:
            return
        worker = Worker(make_thumbnail, self.ffmpeg.ffmpeg, info.path, info.duration_s, self.config.cache_dir)

        def done(path, w=worker, media=info.path) -> None:
            self._pending.discard(w)
            if path is not None:
                from PySide6.QtGui import QImage

                image = QImage(str(path))
                if not image.isNull():
                    self.media_panel.set_thumbnail(media, image)

        worker.signals.finished.connect(done)
        worker.signals.failed.connect(lambda _exc, w=worker: self._pending.discard(w))
        self._pending.add(worker)
        self.pool.start(worker)

    def _on_media_imported(self, info: MediaInfo) -> None:
        log.info("Imported %s (%.2fs)", info.path, info.duration_s)
        added = self.project.add_media(
            MediaRef(path=str(info.path), size_bytes=info.size_bytes, duration_s=info.duration_s)
        )
        if added:
            self._set_dirty(True)
        self.media_panel.add_media(info)
        self._make_thumbnail(info)
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
            self._show_timeline(None)
        elif self.preview.media is None or self.preview.media.path != info.path:
            self._loading_composition = True
            try:
                self.preview.load(info)  # also tells the controller the source size
                if self.project.composition is None and info.video:
                    # First video of the project: start from a sensible default.
                    self.project.composition = default_composition(*info.video.display_size)
                    self.composition.set_composition(self.project.composition)
                    self._main_layout = self.project.composition
            finally:
                self._loading_composition = False
            self._show_timeline(info)

    def _show_timeline(self, info: MediaInfo | None) -> None:
        """Load (or create) the edit of ``info`` and start a fresh undo history."""
        timeline = None
        if info is not None:
            duration_us = seconds_to_us(info.duration_s)
            timeline = self.project.timeline_for(str(info.path))
            # A stored edit is only valid for the same source length.
            if timeline is None or abs(timeline.duration_us - duration_us) > 50_000:
                timeline = Timeline.for_source(duration_us)
                self.project.set_timeline(str(info.path), timeline)
            self.timeline.fps = (info.video.avg_fps or info.video.fps) if info.video else 0
        transcript = self.project.transcript_for(str(info.path)) if info is not None else None
        censor = self.project.censor_for(str(info.path)) if info is not None else None
        self.censor.duration_us = timeline.duration_us if timeline is not None else 0
        self._loading_composition = True
        try:
            self.timeline.set_range(None)
            self.timeline.select(None)
            self.timeline.set_timeline(timeline)
            self.timeline.set_playhead(0)
            self.transcript.set_transcript(transcript)
            self.censor.set_current(None)
            self.censor.set_state(censor)
        finally:
            self._loading_composition = False
        self._preview_stop_us = None
        self.censor_panel.set_has_transcript(transcript is not None)
        self._refresh_censor_view()
        self.transcript_panel.set_has_audio(info is not None and info.has_audio)
        self.transcript_panel.rebuild()
        self.history = self.session.history_for(info.path if info is not None else None, self._edit_state())
        if hasattr(self, "highlights"):
            self.highlights.show_for_current()
        self._update_edit_actions()
        view = self.timeline_panel.view
        view.set_waveform(None)
        view.request_fit()
        if info is not None and info.has_audio and self.ffmpeg is not None:
            worker = Worker(load_waveform, info.path, self.ffmpeg.ffmpeg, self.config.cache_dir)
            worker.signals.finished.connect(lambda wave, w=worker, p=info.path: self._waveform_done(w, p, wave))
            worker.signals.failed.connect(lambda exc, w=worker, p=info.path: self._waveform_done(w, p, None, exc))
            self._pending.add(worker)
            self.pool.start(worker)

    def _waveform_done(self, worker: Worker, path: Path, waveform, error=None) -> None:
        self._pending.discard(worker)
        if error is not None:
            log.warning("Waveform failed for %s: %s", path, error)
            return
        current = self.preview.media
        if current is not None and current.path == path:
            self.timeline_panel.view.set_waveform(waveform)

    # -------------------------------------------------------- edit history
    # ------------------------------------------------------ layout per part
    @property
    def main_layout(self):
        """The project's layout; parts of the clip without their own use it."""
        return self._main_layout

    def _layout_part(self):
        """(timeline, index) of the part under the playhead, or (None, None)."""
        t = self.timeline.timeline
        if t is None:
            return None, None
        return t, t.layout_index_at(self.timeline.playhead_us)

    def _refresh_layout(self) -> None:
        """Show the layout of the part under the playhead in the editor and preview."""
        t, index = self._layout_part()
        main = self.main_layout
        effective = main
        if t is not None and t.segments[index].layout is not None:
            effective = t.segments[index].layout
        if effective != self.composition.composition:
            previous, self._loading_composition = self._loading_composition, True
            try:
                self.composition.set_composition(effective)
            finally:
                self._loading_composition = previous
        if t is not None:
            self.composition_panel.set_part_info(index, len(t.segments), t.segments[index].layout is not None,
                                                 t.has_layouts)
        else:
            self.composition_panel.set_part_info(0, 1, False, False)

    def _on_layout_changed(self) -> None:
        """The user edited the layout: store it for the part under the playhead
        when the clip has several parts, else as the project's layout."""
        if self._loading_composition:
            return
        comp = self.composition.composition
        t, index = self._layout_part()
        if t is not None and (len(t.segments) > 1 or t.segments[index].layout is not None):
            current = t.segments[index].layout or self.main_layout
            if comp != current:
                self._loading_composition = True
                try:
                    self.timeline.set_timeline(t.with_segment_layout(index, comp))
                finally:
                    self._loading_composition = False
                self._refresh_layout()
        else:
            self._main_layout = comp
        self._on_edit_changed("composition")

    def apply_layout_to_all_parts(self) -> None:
        t = self.timeline.timeline
        if t is None:
            return
        self._main_layout = self.composition.composition
        self._commit(timeline=t.without_layouts())
        self._refresh_layout()

    def _edit_state(self):
        return (
            self.main_layout,
            self.timeline.timeline,
            self.transcript.transcript,
            self.censor.state,
        )

    def _on_edit_changed(self, key: object) -> None:
        """A controller changed: store it in the project and the undo history."""
        if self._loading_composition:
            return
        state = self._edit_state()
        if not self.history.record(state, key=key):
            return
        self._store_edit(*state)
        self._update_edit_actions()

    def _store_edit(self, composition, timeline, transcript, censor) -> None:
        self.project.composition = composition
        media = self.preview.media
        if media is not None:
            if timeline is not None:
                self.project.set_timeline(str(media.path), timeline)
            self.project.set_transcript(str(media.path), transcript)
            self.project.set_censor(str(media.path), censor)
        self._set_dirty(True)

    def _apply_history_state(self, state) -> None:
        if state is None:
            return
        composition, timeline, transcript, censor = state
        self._loading_composition = True
        try:
            self._main_layout = composition
            self.timeline.set_timeline(timeline)
            self.transcript.set_transcript(transcript)
            self.censor.set_state(censor)
            self._refresh_layout()
        finally:
            self._loading_composition = False
        self._store_edit(composition, timeline, transcript, censor)
        self._update_edit_actions()

    def _commit(self, timeline=None, transcript=None, censor=None, *, set_censor: bool = False) -> None:
        """Change several controllers as ONE undo step."""
        self._loading_composition = True
        try:
            if timeline is not None:
                self.timeline.set_timeline(timeline)
            if transcript is not None:
                self.transcript.set_transcript(transcript)
            if set_censor or censor is not None:
                self.censor.set_state(censor)
        finally:
            self._loading_composition = False
        self._on_edit_changed(None)

    # --------------------------------------------------------------- censor
    def _refresh_censor_view(self) -> None:
        state = self.censor.state
        self.timeline_panel.view.set_censor(state, self.censor.current)
        applied = state is None or state.is_applied(self.timeline.timeline)
        self.censor_panel.set_applied(applied)

    def _on_censor_current(self) -> None:
        self._refresh_censor_view()
        state, current = self.censor.state, self.censor.current
        sug = state.get(current) if state is not None and current is not None else None
        if sug is not None:
            self.preview.seek(us_to_ms(sug.start_us))

    def _redetect(self, transcript=None):
        transcript = transcript if transcript is not None else self.transcript.transcript
        return detect(
            transcript,
            self.censor_dictionary,
            self.censor_panel.settings(),
            previous=self.censor.state,
            duration_us=self.censor.duration_us,
        )

    def detect_censor(self) -> None:
        if self.transcript.transcript is None:
            return
        state = self._redetect()
        self.censor.set_state(state)
        self.censor_panel.rebuild()  # also refreshes the summary when nothing changed
        self.dock_raise(self.censor_dock)
        pending = state.count("pending")
        self.statusBar().showMessage(
            f"Censor: {len(state.suggestions)} found, {pending} to review. Nothing is muted until you apply.", 6000
        )

    def apply_censor(self) -> None:
        state, timeline = self.censor.state, self.timeline.timeline
        if state is None or timeline is None:
            return
        timeline, state = state.apply(timeline)
        self._commit(timeline=timeline, censor=state)
        self.statusBar().showMessage(
            f"Censor applied: {len(state.applied_mute_ids)} mute region(s) on the timeline.", 5000
        )

    def remove_censor_mutes(self) -> None:
        state, timeline = self.censor.state, self.timeline.timeline
        if state is None or timeline is None:
            return
        timeline, state = state.remove_applied(timeline)
        self._commit(timeline=timeline, censor=state)

    def _save_censor_dictionary(self) -> None:
        try:
            self.censor_dictionary.save(self.censor_dictionary_path)
        except OSError as exc:
            QMessageBox.warning(self, "Censor dictionary", f"Could not save the dictionary:\n{exc}")

    def exclude_censor_word(self, word: str) -> None:
        if self.censor_dictionary.add_exclusion(word):
            self._save_censor_dictionary()
        if self.censor.state is not None:
            self.censor.set_state(self._redetect())
        self.statusBar().showMessage(f"“{word}” will not be censored any more.", 5000)

    def edit_censor_word(self, segment_id: int, index: int, text: str) -> None:
        transcript = self.transcript.transcript
        if transcript is None:
            return
        segment = transcript.segment(segment_id)
        if segment is not None and 0 <= index < len(segment.words):
            self.learn_correction(segment.words[index].text, text)
        transcript = transcript.with_word_text(segment_id, index, text)
        self._commit(transcript=transcript, censor=self._redetect(transcript), set_censor=True)
        self.censor_panel.rebuild()

    def edit_censor_dictionary(self) -> None:
        dialog = DictionaryDialog(self.censor_dictionary, self)
        if dialog.exec() != DictionaryDialog.DialogCode.Accepted:
            return
        self.censor_dictionary = dialog.result_dictionary()
        self._save_censor_dictionary()
        if self.censor.state is not None:
            self.censor.set_state(self._redetect())

    def preview_range(self, start_us: int, end_us: int) -> None:
        """Play a little before and after a range, then pause."""
        if self.preview.media is None:
            return
        self.preview.seek(us_to_ms(max(0, start_us - 800_000)))
        self._preview_stop_us = end_us + 500_000
        self.preview.play()

    def _check_preview_stop(self, us: int) -> None:
        if self._preview_stop_us is not None and us >= self._preview_stop_us:
            self._preview_stop_us = None
            self.preview.pause()

    # --------------------------------------------------------------- export
    def _start_encoder_detection(self) -> None:
        worker = Worker(detect_encoders, self.ffmpeg.ffmpeg)
        worker.signals.finished.connect(lambda found, w=worker: self._encoders_detected(w, found))
        worker.signals.failed.connect(lambda exc, w=worker: self._encoders_detected(w, []))
        self._pending.add(worker)
        self.pool.start(worker)

    def _encoders_detected(self, worker: Worker, found) -> None:
        self._pending.discard(worker)
        self.encoders = list(found)
        dialog = getattr(self, "_export_dialog", None)
        if dialog is not None:
            dialog.set_encoders(self.encoders)

    def export_video(self) -> None:
        media, timeline = self.preview.media, self.timeline.timeline
        composition = self.main_layout
        if media is None or media.video is None or timeline is None or self.ffmpeg is None:
            return
        if self.preview.is_playing:
            self.preview.pause()
        defaults = RenderSettings(width=composition.output_w, height=composition.output_h)
        settings = RenderSettings.from_dict(self.config.export_settings) if self.config.export_settings else defaults
        output = default_output_path(Path(media.path), self.config.export_dir, settings.extension)
        self._refresh_subtitles()
        dialog = ExportDialog(
            media, composition, timeline, self.ffmpeg, settings, output, self.encoders, self,
            upload=self.upload_to_youtube,
            captions=self.captions, subtitle_style=self.subtitles_panel.style(),
        )
        self._export_dialog = dialog
        try:
            dialog.exec()
        finally:
            self._export_dialog = None
        self.config.export_settings = dialog.settings().to_dict()
        self.config.export_dir = str(dialog.output_path().parent)
        if dialog.result_path is not None:
            self._rendered = (Path(dialog.result_path), self._render_state())
            self.statusBar().showMessage(f"Exported {dialog.result_path}", 8000)
        self._refresh_steps()

    # -------------------------------------------------------------- youtube
    def manage_youtube_accounts(self) -> None:
        dialog = YouTubeAccountsDialog(self.youtube, self)
        dialog.exec()
        self.config.save()  # account list (no secrets) and the active channel

    def upload_to_youtube(self, path: Path | None = None) -> None:
        if path is None:
            start = self.config.export_dir or self.config.last_import_dir
            file, _ = QFileDialog.getOpenFileName(
                self, "Upload to YouTube", start, "Rendered video (*.mp4 *.mkv *.mov *.webm);;All files (*)"
            )
            if not file:
                return
            path = Path(file)
        sources = {str(Path(m.path)).casefold() for m in self.project.media}
        if str(Path(path)).casefold() in sources:
            QMessageBox.warning(
                self, "Upload to YouTube",
                "This is a source clip of the project. Export the edited video first (Ctrl+E) and upload that.",
            )
            return
        dialog = UploadDialog(
            self.youtube, self.upload_history, Path(path), self.ffmpeg.ffprobe if self.ffmpeg else None,
            self.config.youtube_defaults, self.manage_youtube_accounts, self,
            metadata=self.project.metadata,
            captions=self.captions,
            captions_language=getattr(self.transcript.transcript, "language", "") or "",
        )
        dialog.exec()
        self.config.youtube_defaults = dialog.defaults()
        if dialog.result is not None:
            self._published = str(Path(path))
            self.statusBar().showMessage(f"Uploaded to YouTube: {dialog.result.url}", 10000)
        self._refresh_steps()

    # --------------------------------------------------------------- twitch
    def import_from_twitch(self, url: str = "", ydl_factory=None) -> bool:
        dialog = TwitchImportDialog(self.config.twitch_folder, self.config.twitch_autofill,
                                    self.config.twitch_add_to_library, url, self, ydl_factory=ydl_factory)
        accepted = dialog.exec() == dialog.DialogCode.Accepted
        if not dialog.info_only_check.isChecked():
            self.config.twitch_folder = dialog.folder
            self.config.twitch_autofill = dialog.autofill_check.isChecked()
        self.config.twitch_add_to_library = dialog.add_to_library
        if not accepted or dialog.result is None:
            return False
        path, clip = dialog.result
        self.apply_twitch_result(path, clip, dialog.autofill, dialog.add_to_library)
        return True

    def apply_twitch_result(self, path, clip, fill: bool, add_to_library: bool) -> None:
        """Import the downloaded clip and fill the project's metadata."""
        if path is not None:
            self.import_files([str(path)])
        if not fill:
            return
        result = autofill(self.project.metadata, clip, self.metadata_library, add_to_library=add_to_library)
        self.metadata_panel.refresh_library()
        self.metadata_panel.set_metadata(result.metadata)
        self._metadata_changed(result.metadata)
        note = f"Metadata filled from Twitch: {clip.channel or '?'}"
        if clip.game:
            note += f" • {clip.game}"
        if result.added:
            note += " • added to the library: " + ", ".join(result.added)
        self.statusBar().showMessage(note, 10000)

    # ------------------------------------------------------------- metadata
    def open_metadata_library(self, kind: str = "", entity_id: str = "") -> None:
        dialog = LibraryDialog(self.metadata_library, kind or "streamer", self)
        if kind and entity_id:
            dialog.edit(kind, entity_id)
        dialog.library_changed.connect(self.metadata_panel.refresh_library)
        dialog.exec()
        self.metadata_panel.refresh_library()

    def _metadata_changed(self, metadata) -> None:
        if metadata != self.project.metadata:
            self.project.metadata = metadata
            self._set_dirty(True)

    # -------------------------------------------------------- transcription
    def start_transcription(self, options) -> None:
        media = self.preview.media
        if media is None or not media.has_audio or self.ffmpeg is None:
            return
        if self._transcribe_task is not None:
            return
        # Remember the choices for next time.
        self._remember_transcribe_preferences(options)
        task = CancellableTask(transcribe_file, self.engine, media.path, self.ffmpeg.ffmpeg, options)
        task.signals.progress.connect(self.transcript_panel.set_progress)
        task.signals.finished.connect(self._transcription_done)
        task.signals.failed.connect(self._transcription_failed)
        task.signals.cancelled.connect(self._transcription_cancelled)
        self._transcribe_task = task
        self._transcribe_path = media.path
        self.transcript_panel.set_busy(True, "Starting…")
        self.dock_raise(self.transcript_dock)
        log.info("Transcribing %s with %s", media.path, options)
        self.pool.start(task)

    def _remember_transcribe_preferences(self, options) -> None:
        panel = self.transcript_panel
        self.config.transcribe_model = options.model
        self.config.transcribe_language = options.language
        self.config.transcribe_vocabulary = panel.vocabulary_edit.text()  # without the learned words
        self.config.transcribe_uncensored = options.uncensored
        self.config.transcribe_slang = options.slang
        quality = panel.quality_preferences()
        self.config.transcribe_clean_audio = quality["clean"]
        self.config.transcribe_separate_voice = quality["separate"]
        self.config.transcribe_refine_timing = quality["refine"]
        self.config.transcribe_vad = quality["vad"]
        self.config.transcribe_accurate = quality["accurate"]
        self.config.transcribe_custom_model = quality["custom_model"]

    # ------------------------------------------------- learning / fragments
    def learn_correction(self, old: str, new: str) -> None:
        words = learned_words(old, new)
        if words:
            self.config.transcribe_learned = merge_learned(self.config.transcribe_learned, words)
            self.transcript_panel.set_learned(self.config.transcribe_learned)

    def forget_learned(self) -> None:
        self.config.transcribe_learned = []
        self.transcript_panel.set_learned([])

    def _retranscribe_target(self) -> tuple[int, int] | None:
        """Selected timeline range, else the phrase under the playhead."""
        if self.timeline.range is not None:
            return self.timeline.range
        t = self.transcript.transcript
        seg = t.segment_at(self.timeline.playhead_us) if t is not None else None
        return (seg.start_us, seg.end_us) if seg is not None else None

    def _update_retranscribe(self) -> None:
        self.transcript_panel.set_range_available(self._retranscribe_target() is not None)

    def retranscribe_range(self, options) -> None:
        media, t = self.preview.media, self.transcript.transcript
        target = self._retranscribe_target()
        if media is None or t is None or target is None or self.ffmpeg is None or self._transcribe_task is not None:
            return
        start, end = t.covering_range(*target)
        # Decode a little more audio so words at the edges are not cut, but replace only
        # the covered phrases: neighbours stay as they are.
        pad = 200_000
        audio_start, audio_end = max(0, start - pad), min(t.duration_us or end + pad, end + pad)
        self._remember_transcribe_preferences(options)
        task = CancellableTask(
            transcribe_range, self.engine, media.path, self.ffmpeg.ffmpeg, options, audio_start, audio_end
        )
        task.signals.progress.connect(self.transcript_panel.set_progress)
        task.signals.finished.connect(lambda segs, s=start, e=end, p=media.path: self._range_done(p, s, e, segs))
        task.signals.failed.connect(self._transcription_failed)
        task.signals.cancelled.connect(self._transcription_cancelled)
        self._transcribe_task = task
        self._transcribe_path = media.path
        self.transcript_panel.set_busy(True, "Re-transcribing the selection…")
        log.info("Re-transcribing %s %d-%d us with %s", media.path, start, end, options)
        self.pool.start(task)

    def _cleaned(self, transcript):
        """Apply the user's replacement rules and capitalisation to fresh recognition results."""
        from core.transcription.cleanup import clean_transcript, parse_rules

        rules = parse_rules(self.config.transcribe_rules)
        if not rules and not self.config.transcribe_capitalise:
            return transcript, 0
        return clean_transcript(transcript, rules, self.config.transcribe_capitalise)

    def edit_replacement_rules(self) -> None:
        from ui.transcript.rules_dialog import RulesDialog

        t = self.transcript.transcript
        dialog = RulesDialog(self.config.transcribe_rules, self.config.transcribe_capitalise, t is not None, self)
        if dialog.exec() != RulesDialog.DialogCode.Accepted:
            return
        self.apply_replacement_rules(dialog.rules(), dialog.capitalise_check.isChecked(),
                                     dialog.apply_check.isChecked())

    def apply_replacement_rules(self, rules: str, capitalise: bool, apply_now: bool) -> int:
        self.config.transcribe_rules = rules
        self.config.transcribe_capitalise = capitalise
        t = self.transcript.transcript
        if not apply_now or t is None:
            return 0
        cleaned, count = self._cleaned(t)
        if cleaned != t:
            self.transcript.set_transcript(cleaned)  # undoable
        self.statusBar().showMessage(f"Replacement rules: {count} replacement{'s' if count != 1 else ''}.", 8000)
        return count

    def find_speakers(self, count: int | None = None) -> None:
        from core.transcription.diarization import find_speakers_in_file

        media, t = self.preview.media, self.transcript.transcript
        if media is None or t is None or self.ffmpeg is None or self._transcribe_task is not None:
            return
        task = CancellableTask(find_speakers_in_file, self.engine.models_dir, media.path, self.ffmpeg.ffmpeg, t,
                               count)
        task.signals.progress.connect(self.transcript_panel.set_progress)
        task.signals.finished.connect(lambda labels, p=media.path, old=t: self._speakers_done(p, old, labels))
        task.signals.failed.connect(self._transcription_failed)
        task.signals.cancelled.connect(self._transcription_cancelled)
        self._transcribe_task = task
        self._transcribe_path = media.path
        self.transcript_panel.set_busy(True, "Finding speakers…")
        self.dock_raise(self.transcript_dock)
        log.info("Finding speakers in %s (count %s)", media.path, count)
        self.pool.start(task)

    def _speakers_done(self, path, old, labels) -> None:
        self._finish_transcription()
        media, t = self.preview.media, self.transcript.transcript
        if media is None or media.path != path or t is not old:
            self.transcript_panel.set_busy(False, "The transcript changed meanwhile; run Find speakers again.")
            return
        updated = t.with_speakers(labels)
        self.transcript.set_transcript(updated)  # undoable
        n = updated.speaker_count
        self.transcript_panel.set_busy(False, f"Found {n} speaker{'s' if n != 1 else ''}." if n else
                                       "No speech to analyse.")

    def _range_done(self, path, start_us: int, end_us: int, segments) -> None:
        self._finish_transcription()
        media, t = self.preview.media, self.transcript.transcript
        if media is None or media.path != path or t is None:
            self.transcript_panel.set_busy(False, "The clip changed; the result was dropped.")
            return
        segments = tuple(s for s in segments if start_us <= (s.start_us + s.end_us) // 2 < end_us)
        segments = self._cleaned(replace(t, segments=segments))[0].segments
        updated = t.with_range_replaced(start_us, end_us, segments)
        self.transcript.set_transcript(updated)  # undoable
        text = " ".join(s.text for s in segments) or "(no speech found)"
        self.transcript_panel.set_busy(False, f"Re-transcribed {len(segments)} phrase(s): {text[:160]}")

    def dock_raise(self, dock: QDockWidget) -> None:
        dock.show()
        dock.raise_()

    def cancel_transcription(self) -> None:
        if self._transcribe_task is not None:
            self._transcribe_task.cancel()
            self.transcript_panel.set_progress(self.transcript_panel.progress.value() / 1000, "Cancelling…")

    def _finish_transcription(self) -> Path | None:
        path, self._transcribe_task, self._transcribe_path = self._transcribe_path, None, None
        self.transcript_panel.set_downloaded_models(downloaded_models(self.engine.models_dir))
        return path

    def _transcription_done(self, transcript) -> None:
        path = self._finish_transcription()
        device = self.engine.device_used.upper() or "CPU"
        message = f"Done on {device}: {len(transcript.segments)} segments."
        if self.engine.notice:
            message += "\n" + self.engine.notice
        self.transcript_panel.set_busy(False, message)
        media = self.preview.media
        if path is None:
            return
        transcript = self._cleaned(transcript)[0]
        if media is not None and media.path == path:
            self.transcript.set_transcript(transcript)  # undoable like any edit
        else:
            # The user switched clips meanwhile: store it for that clip.
            self.project.set_transcript(str(path), transcript)
            self._set_dirty(True)

    def _transcription_failed(self, error) -> None:
        self._finish_transcription()
        message = str(error) if isinstance(error, TranscriptionError) else f"Unexpected error: {error}"
        log.error("Transcription failed: %s", message)
        self.transcript_panel.set_busy(False, "Transcription failed.")
        QMessageBox.warning(self, "Transcription failed", message)

    def _transcription_cancelled(self) -> None:
        self._finish_transcription()
        self.transcript_panel.set_busy(False, "Cancelled.")

    def undo(self) -> None:
        self._apply_history_state(self.history.undo())

    def redo(self) -> None:
        self._apply_history_state(self.history.redo())

    def _update_edit_actions(self) -> None:
        t = self.timeline.timeline
        has = t is not None
        selected, rng = self.timeline.selected, self.timeline.range
        self.act_undo.setEnabled(self.history.can_undo)
        self.act_redo.setEnabled(self.history.can_redo)
        self.act_split.setEnabled(has)
        self.act_delete.setEnabled(has and (selected is not None or rng is not None))
        self.act_trim.setEnabled(has and rng is not None)
        self.act_mute.setEnabled(has and rng is not None)
        self.act_restore.setEnabled(has and t.output_us < t.duration_us)
        self.act_merge_mutes.setEnabled(has and len(t.merged_mutes()) < len(t.mutes))
        media = self.preview.media
        self.act_export.setEnabled(
            has and self.ffmpeg is not None and media is not None and media.video is not None
        )

    def _on_remove_media(self, info: MediaInfo) -> None:
        self.project.remove_media(str(info.path))
        self.session.forget(info.path)
        self.media_panel.remove_media(info.path)
        self._set_dirty(True)

    # ------------------------------------------------------------ project
    def new_project(self) -> None:
        if not self._confirm_discard():
            return
        self.project = Project()
        self.project_path = None
        self.session.reset()
        self.metadata_panel.set_metadata(self.project.metadata)
        self.subtitles_panel.set_settings(self.project.subtitles)
        self.media_panel.clear()
        self.preview.unload()
        self.properties_panel.show_media(None)
        self._apply_project_composition()
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
        self.session.load_for(path)
        self._load_project(project, path)
        self._set_dirty(False)

    def restore_recovery(self, entry) -> bool:
        """Open a crash-recovery copy; it stays unsaved until the user saves."""
        try:
            data, histories = self.session.store.load(entry)
            project = Project.from_dict(data)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            QMessageBox.warning(self, "Cannot restore", f"{entry.project_name}\n\n{exc}")
            return False
        self.session.reset(histories)
        self.session.adopt(entry)
        self._load_project(project, Path(entry.original_path) if entry.original_path else None)
        self._set_dirty(True)
        self.statusBar().showMessage(f"Restored unsaved work of {entry.project_name}", 6000)
        return True

    def _load_project(self, project: Project, path: Path | None) -> None:
        self.project = project
        self.project_path = path
        self.metadata_panel.set_metadata(project.metadata)
        self.subtitles_panel.set_settings(project.subtitles)
        self.media_panel.clear()
        self.preview.unload()
        self._apply_project_composition()
        missing = [m.path for m in project.media if not Path(m.path).is_file()]
        self.import_files([m.path for m in project.media if Path(m.path).is_file()])
        if missing:
            QMessageBox.warning(
                self, "Missing media", "These source files were not found:\n\n" + "\n".join(missing)
            )

    def _apply_project_composition(self) -> None:
        """Push the project's composition into the editor without marking it dirty."""
        self._loading_composition = True
        try:
            self._main_layout = self.project.composition or default_composition(*self.composition.source_size)
            self.composition.set_composition(self._main_layout)
            self._refresh_layout()
        finally:
            self._loading_composition = False
        self.history = History(self._edit_state())
        self._update_edit_actions()

    def save(self) -> bool:
        if self.project_path is None:
            return self.save_as()
        try:
            self.project_path = save_project(self.project, self.project_path)
        except OSError as exc:
            QMessageBox.warning(self, "Save failed", str(exc))
            return False
        self._set_dirty(False)
        self.session.on_saved()
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
        if answer == QMessageBox.StandardButton.Discard:
            self.session.discard_recovery()
            return True
        return False

    @property
    def is_dirty(self) -> bool:
        return self._dirty

    def _set_dirty(self, dirty: bool) -> None:
        self._dirty = dirty
        self._update_title()
        self._refresh_steps()

    def _update_title(self) -> None:
        name = self.project_path.name if self.project_path else "Untitled"
        self.setWindowTitle(f"{name}{' *' if self._dirty else ''} — {APP_NAME}")
        if hasattr(self, "project_label"):
            self.project_label.setText(f"{self.project_path.stem if self.project_path else 'Untitled project'}"
                                       f"{'  •  unsaved' if self._dirty else ''}")

    # ---------------------------------------------------------- subtitles
    def _subtitles_changed(self, settings: SubtitleSettings) -> None:
        if settings != self.project.subtitles:
            self.project.subtitles = settings
            self._set_dirty(True)
        self._refresh_subtitles()

    def _refresh_subtitles(self) -> None:
        settings = self.subtitles_panel.settings()
        transcript, timeline = self.transcript.transcript, self.timeline.timeline
        self.subtitles_panel.set_has_transcript(transcript is not None)
        self.captions = build_captions(transcript, timeline, settings.style, settings.edits)
        self.subtitles_panel.set_captions(self.captions)
        self.preview.output_view.set_subtitles(self.captions, settings.style)
        self._update_subtitle_time(self.timeline.playhead_us)

    def _update_subtitle_time(self, source_us: int) -> None:
        timeline = self.timeline.timeline
        out = timeline.source_to_output(source_us) if timeline is not None else None
        self.preview.output_view.set_subtitle_time(out)

    def _seek_output(self, output_us: int) -> None:
        timeline = self.timeline.timeline
        if timeline is not None:
            self.preview.seek(us_to_ms(timeline.output_to_source(output_us)))

    def split_caption_at_playhead(self) -> bool:
        """Start a new caption at the word under (or right after) the playhead."""
        timeline = self.timeline.timeline
        if timeline is None or not self.captions:
            return False
        now = timeline.source_to_output(self.timeline.playhead_us)
        words = [w for c in self.captions for w in c.words]
        word = next((w for w in words if w.start_us <= now < w.end_us), None) \
            or next((w for w in words if w.start_us >= now), None)
        if word is None:
            return False
        edits = self.subtitles_panel.settings().edits.split_at(word.source_us)
        self.subtitles_panel.set_edits(edits)
        return True

    def save_subtitles(self, path: Path | None = None) -> Path | None:
        if not self.captions:
            return None
        if path is None:
            media = self.preview.media
            base = Path(self.config.export_dir or (media.path.parent if media else Path.home()))
            name = (media.path.stem if media else "subtitles") + ".srt"
            file, _ = QFileDialog.getSaveFileName(
                self, "Save Subtitles", str(base / name), "SubRip (*.srt);;WebVTT (*.vtt)"
            )
            if not file:
                return None
            path = Path(file)
        try:
            write_subtitles(path, self.captions)
        except OSError as exc:
            QMessageBox.warning(self, "Save Subtitles", f"Could not save {path.name}:\n{exc}")
            return None
        self.statusBar().showMessage(f"Saved {path}", 8000)
        return path

    # --------------------------------------------------------- appearance
    def set_appearance(self, theme: str | None = None, mica: bool | None = None, ask: bool = True) -> bool:
        """Store the theme / Mica choice; it applies after a restart. Returns
        True when anything changed."""
        changed = False
        if theme is not None and theme != self.config.theme:
            self.config.theme = theme
            changed = True
        if mica is not None and bool(mica) != self.config.mica:
            self.config.mica = bool(mica)
            changed = True
        if not changed:
            return False
        self.config.save()
        if ask:
            answer = QMessageBox.question(
                self, "Restart to apply",
                "The new look is used after a restart. Restart Twitch Shorts Studio now?\n\n"
                "Your project is kept: you will be asked to save it if needed.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self.restart_requested = True
                if not self.close():
                    self.restart_requested = False
        else:
            self.statusBar().showMessage("The new look is used after a restart.", 8000)
        return True

    # ------------------------------------------------------------- layout
    def set_autosave(self, enabled: bool | None = None, minutes: int | None = None) -> None:
        if enabled is not None:
            self.config.autosave_enabled = bool(enabled)
        if minutes in AUTOSAVE_CHOICES:
            self.config.autosave_minutes = minutes
        for action in self.autosave_actions.values():
            action.setEnabled(self.config.autosave_enabled)
        self.session.apply_settings()

    def reset_layout(self) -> None:
        self.set_page(EDIT)
        for dock in (self.media_dock, self.properties_dock, self.transcript_dock, self.highlights_dock, self.subtitles_dock,
                     self.censor_dock, self.timeline_dock):
            dock.setFloating(False)
            dock.show()
        self.restoreState(self._default_state, LAYOUT_VERSION)
        self._update_dock_titles()

    def _restore_layout(self) -> None:
        if self.config.window_geometry:
            self.restoreGeometry(QByteArray.fromBase64(self.config.window_geometry.encode()))
        if self.config.window_state:
            self.restoreState(QByteArray.fromBase64(self.config.window_state.encode()), LAYOUT_VERSION)

    def _rebuild_recent_menu(self) -> None:
        self.recent_menu.clear()
        for file in self.config.recent_files:
            action = self.recent_menu.addAction(file)
            action.triggered.connect(lambda _=False, f=file: self.import_files([f]))
        self.recent_menu.setEnabled(bool(self.config.recent_files))

    # ------------------------------------------------------------- events
    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        mime = event.mimeData()
        if mime.hasUrls() or (mime.hasText() and parse_clip_url(mime.text())):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        mime = event.mimeData()
        links = [u.toString() for u in mime.urls() if not u.isLocalFile() and parse_clip_url(u.toString())]
        if not links and mime.hasText() and parse_clip_url(mime.text()):
            links = [mime.text()]
        if links:  # a clip link dragged from the browser
            self.import_from_twitch(links[0])
            return
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
        if self._transcribe_task is not None:
            self._transcribe_task.cancel()
        self.edit_playback.shutdown()
        self.listen.stop()
        self.autoframe.cancel()
        player = self.preview.player
        # stop() on a paused player can block inside Qt's FFmpeg backend when
        # there is no audio device; the window is closing, so pausing is enough.
        if player.playbackState() == player.PlaybackState.PlayingState:
            player.pause()
        self.config.window_geometry = bytes(self.saveGeometry().toBase64()).decode()
        self.config.window_state = bytes(self._edit_layout_state().toBase64()).decode()
        self.config.timeline_view = self.timeline_panel.view.view_state()
        self.pool.waitForDone(3000)
        self.session.finish()
        self.metadata_library.close()
        event.accept()

    # --------------------------------------------------------------- help
    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            f"About {APP_NAME}",
            f"<b>{APP_NAME}</b> {APP_VERSION}<br>"
            "Vertical Shorts editor for Twitch clips.<br>Phase 7: YouTube upload.",
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
