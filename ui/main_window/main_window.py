"""Main window: docks, menus, import flow and project save/open."""

from __future__ import annotations

import logging
from dataclasses import replace
import html
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QByteArray, QSize, QThreadPool, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QDragEnterEvent, QDropEvent, QKeySequence
from PySide6.QtWidgets import (
    QDockWidget,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QPushButton,
    QMessageBox,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QToolButton,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from app.config import APP_NAME, APP_VERSION, AppConfig
from core.audio.waveform import load_waveform
from core.censor.dictionary import Dictionary
from core.censor.review import CensorSettings, detect
from core.render.encoders import detect_encoders
from core.render.settings import RenderSettings
from core.metadata.library import Library
from core.youtube.history import UploadHistory, UploadRecord
from core.youtube.metadata import DEFAULT_CATEGORY, PRIVATE, VideoMetadata
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
from core.projects import clip_memory as cm
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
from ui.export.render_queue import QueuePanel, RenderQueue, UploadPlan
from ui.metadata.library_dialog import LibraryDialog
from ui.metadata.metadata_panel import MetadataPanel
from ui.metadata.youtube_preview import YouTubePreview
from ui.subtitles.subtitles_panel import SubtitlesPanel
from ui.theme import icon, palette, windows
from ui.twitch.import_dialog import TwitchImportDialog
from core.twitch.autofill import autofill
from core.twitch.clip import parse_clip_url
from ui.widgets.card import Card
from ui.youtube.accounts_dialog import YouTubeAccountsDialog
from ui.youtube.analytics_panel import AnalyticsPanel
from ui.social.crosspost_panel import CrossPostPanel
from ui.social.published_panel import PublishedPanel
from ui.metadata.variants_card import VariantsCard
from ui.social.guide_panel import INSTAGRAM, TIKTOK, YOUTUBE, GuidePanel
from core.social.service import SocialService
from ui.youtube.upload_dialog import UploadDialog, _upload
from ui.panels.composition_panel import CompositionPanel
from ui.panels.media_panel import MediaPanel
from ui.panels.properties_panel import PropertiesPanel
from ui.panels.timeline_panel import TimelinePanel
from ui.preview.edit_playback import EditPlayback
from ui.preview.preview_panel import PreviewPanel
from ui.highlights.highlights_panel import HighlightsPanel
from ui.main_window.autoframe_runner import AutoframeRunner
from ui.main_window.blur_runner import BlurRunner
from ui.blur.blur_panel import BlurPanel
from ui.main_window.highlights_controller import HighlightsController
from ui.main_window.listen_preview import ListenPreview
from ui.main_window.pages import (
    DATA, EDIT, PAGES, PUBLISH, RENDER, PageBar, ServiceTabs, StepPage, animate_in, page_hero, page_scroll,
)
from ui.main_window.session_keeper import AUTOSAVE_CHOICES, SessionKeeper
from ui.main_window.thumbnail_keeper import ThumbnailKeeper
from ui.thumbnails.thumbnail_editor import ThumbnailEditor
from ui.timeline.timeline_controller import TimelineController
from ui.transcript.transcript_controller import TranscriptController
from ui.transcript.transcript_panel import TranscriptPanel

log = logging.getLogger(__name__)

LAYOUT_VERSION = 11  # bump when the dock arrangement changes

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
        self._translate_task: CancellableTask | None = None
        self._transcribe_path: Path | None = None
        self.censor = CensorController(self)
        self.censor_dictionary_path = config.data_dir / "censor_dictionary.json"
        self.censor_dictionary = Dictionary.load(self.censor_dictionary_path)
        self._preview_stop_us: int | None = None
        self.encoders = None  # usable video encoders, detected in the background
        self.youtube = YouTubeService(config)
        self.social = SocialService(config, self.youtube.store)  # TikTok / Instagram Reels
        self.upload_history = UploadHistory(config.data_dir / "youtube_uploads.json")
        self.clip_memory = cm.ClipMemory(config.data_dir / "clip_memory.json")  # title + cover per clip
        self._memory_source = ""  # the clip whose title and cover are shown
        self._remember_timer = QTimer(self)
        self._remember_timer.setSingleShot(True)
        self._remember_timer.setInterval(700)
        self._remember_timer.timeout.connect(self._remember_clip)  # pause here when listening to a suggestion
        self.metadata_library = Library(config.data_dir / "metadata_library.sqlite")
        self.history: History = History(self._edit_state())
        self.session = SessionKeeper(self)  # autosave + undo history kept between runs

        self.preview = PreviewPanel(self.composition, self)
        # Steps as pages (Resolve-style): the editor, then data, render and publish pages.
        self.page_stack = QStackedWidget(self)
        self.page_stack.addWidget(self.preview)
        # The setup guide opens as a side panel next to any page.
        self.guide_panel = GuidePanel(self.social, self)
        central = QWidget(self)
        central_row = QHBoxLayout(central)
        central_row.setContentsMargins(0, 0, 0, 0)
        central_row.setSpacing(0)
        central_row.addWidget(self.page_stack, 1)
        central_row.addWidget(self.guide_panel)
        self.setCentralWidget(central)
        self._page = EDIT
        self._edit_layout: QByteArray | None = None  # the Edit page's docks while another page is shown
        self._rendered: tuple | None = None  # (path, edit state) of the last export
        self._published: str = ""  # path of the last uploaded export
        self.export_widget: ExportDialog | None = None  # the Render page's form
        self._export_dialog: ExportDialog | None = None
        self.upload_widget: UploadDialog | None = None  # the Publish page's form

        self.media_panel = MediaPanel(self)
        self.properties_panel = PropertiesPanel(self)  # media info
        self.composition_panel = CompositionPanel(self.composition, self.presets, self)
        self.timeline_panel = TimelinePanel(self.timeline, self)
        self.edit_playback = EditPlayback(self.preview, self.timeline, self)
        self.listen = ListenPreview(self)  # Timeline → Listen (noise reduction + loudness)
        self.autoframe = AutoframeRunner(self)  # Composition → Face → Auto-frame
        self.transcript_panel = TranscriptPanel(self.transcript, self)
        self.censor_panel = CensorPanel(self.censor, self)
        self.blur_panel = BlurPanel(self.timeline, self)
        self.blur_runner = BlurRunner(self)  # Blur → Follow object
        self.preview.set_blur_controller(self.timeline)
        self.metadata_panel = MetadataPanel(self.metadata_library, self)
        self.subtitles_panel = SubtitlesPanel(self)
        self.highlights_panel = HighlightsPanel(self)
        self.captions: list = []  # current captions (output time)
        data = QWidget(self)
        data_column = QVBoxLayout(data)
        data_column.setContentsMargins(0, 0, 0, 0)
        data_column.setSpacing(14)
        self.data_hero = page_hero(DATA, "Data", "Title, description and tags for YouTube, made from a template, "
                                   "and the cover of the Short.", "document", data)
        self.clip_published_label = QLabel(self.data_hero)  # "Published: YouTube 21.11 «…»"
        self.clip_published_label.setWordWrap(True)
        self.clip_published_label.setOpenExternalLinks(True)
        self.clip_published_label.hide()
        self.data_hero.body.addWidget(self.clip_published_label)
        data_column.addWidget(self.data_hero)
        data_row = QHBoxLayout()
        data_row.setSpacing(20)
        data_row.addWidget(self.metadata_panel, 1)
        self.youtube_preview = YouTubePreview(data)
        data_row.addWidget(self.youtube_preview, 0, Qt.AlignmentFlag.AlignTop)
        data_column.addLayout(data_row)
        self.thumbnail_editor = ThumbnailEditor(data)
        data_column.addWidget(self.thumbnail_editor)
        self.variants_card = VariantsCard(data)
        data_column.addWidget(self.variants_card)
        self.variants_card.title_chosen.connect(self._use_title_variant)
        self.variants_card.cover_chosen.connect(self._use_cover_variant)
        self.variants_card.refresh_requested.connect(self._make_variants)
        self._cover_variants_task: Worker | None = None
        self.thumbnails = ThumbnailKeeper(self, self.thumbnail_editor)
        self.data_page = page_scroll(data, self, 1240)
        self.page_stack.addWidget(self.data_page)
        self.render_page = StepPage(
            "Render", "Export the edited Short to a video file: format, quality, loudness, subtitles.",
            "", "render", self, step=RENDER)
        self.render_page.action_button.hide()  # the form below has the Render button
        self.render_queue = RenderQueue(ffmpeg, self, uploader=_queued_upload)
        self.queue_panel = QueuePanel(self.render_queue, self.render_page)
        self.render_page.footer.addWidget(self.queue_panel)
        self.render_queue.job_finished.connect(self._queued_render_done)
        self.render_queue.job_uploaded.connect(self._queued_upload_done)
        self.queue_panel.upload_requested.connect(self.upload_to_youtube)
        self.page_stack.addWidget(page_scroll(self.render_page, self, 820))
        self.publish_page = StepPage(
            "Publish", "Post the exported Short to YouTube, TikTok and Instagram Reels with the title, "
            "description and tags from Data.", "Choose another file…", "folder", self, step=PUBLISH, hero_icon="export")
        self.publish_page.action_button.setObjectName("")
        self.service_tabs = ServiceTabs(self.publish_page)
        self.publish_page.footer.addWidget(self.service_tabs)
        # YouTube: the upload form (put into its own slot) and the channel's analytics.
        youtube_tab = QWidget(self.service_tabs)
        youtube_column = QVBoxLayout(youtube_tab)
        youtube_column.setContentsMargins(0, 0, 0, 0)
        youtube_column.setSpacing(14)
        youtube_bar = QHBoxLayout()
        youtube_note = QLabel("Upload as a Short to your channel. Pick the account in “Where”.", youtube_tab)
        youtube_note.setObjectName("muted")
        youtube_bar.addWidget(youtube_note, 1)
        youtube_guide = QPushButton("Guide", youtube_tab)
        youtube_guide.setToolTip("How to connect YouTube (opens on the right)")
        youtube_guide.clicked.connect(lambda: self.guide_panel.toggle_service(YOUTUBE))
        youtube_bar.addWidget(youtube_guide)
        youtube_column.addLayout(youtube_bar)
        self.publish_page.slot = QVBoxLayout()  # the upload form lands here
        self.publish_page.slot.setContentsMargins(0, 0, 0, 0)
        youtube_column.addLayout(self.publish_page.slot)
        self.analytics_panel = AnalyticsPanel(youtube_tab)
        youtube_column.addWidget(self.analytics_panel)
        self.analytics_panel.refresh_requested.connect(self.refresh_analytics)
        self._analytics_task: CancellableTask | None = None
        self._analytics_loaded = ""  # channel whose numbers are shown
        self._best_time = None  # when the channel's Shorts did best (core.youtube.best_time)
        self.service_tabs.add(youtube_tab, "YouTube", "logo_youtube")
        # TikTok and Instagram Reels: one posting card each.
        self.tiktok_panel = CrossPostPanel(self.social, self.config, TIKTOK, self.service_tabs)
        self.reels_panel = CrossPostPanel(self.social, self.config, INSTAGRAM, self.service_tabs)
        for panel, name, logo in ((self.tiktok_panel, "TikTok", "logo_tiktok"),
                                  (self.reels_panel, "Instagram Reels", "logo_instagram")):
            panel.cover_ms = self._thumbnail_ms
            panel.guide_requested.connect(self.guide_panel.toggle_service)
            panel.posted.connect(lambda _r: self._save_config_quietly())
            panel.posted.connect(self._social_posted)
            self.service_tabs.add(panel, name, logo)
        self.published_panel = PublishedPanel(self.clip_memory, self.service_tabs)
        self.published_panel.open_clip_requested.connect(self._open_published_clip)
        self.published_panel.refresh_requested.connect(self._refresh_published_numbers)
        self.service_tabs.add(self.published_panel, "History", "library")
        self.guide_panel.connections_changed.connect(self._social_connections_changed)
        self.guide_panel.youtube_accounts_requested.connect(self.manage_youtube_accounts)
        self.page_stack.addWidget(page_scroll(self.publish_page, self, 960))

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
        self.blur_dock = self._add_dock("Blur", "dock_blur", self.blur_panel, Qt.DockWidgetArea.RightDockWidgetArea)
        self.tabifyDockWidget(self.censor_dock, self.blur_dock)
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
        from ui.main_window.update_controller import UpdateController

        self.update_installer = None  # set when "Install and restart" closes the window
        self.updates = UpdateController(self)  # Help → Check for Updates (bootstrap starts the quiet check)
        self.help_menu.insertActions(self.act_about, [self.updates.act_check, self.updates.act_auto])
        self._restore_layout()
        self._update_dock_titles()
        self.timeline_panel.view.restore_view_state(self.config.timeline_view)
        self._update_title()
        self._refresh_steps()
        self._migrate_clip_memory()

        if self.ffmpeg is not None:
            self._start_encoder_detection()
        self.version_button = QToolButton(self)
        self.version_button.setObjectName("versionButton")
        self.version_button.setText(f"v{APP_VERSION}")
        self.version_button.setToolTip(f"About {APP_NAME}")
        self.version_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.version_button.clicked.connect(self._show_about)
        self.statusBar().addPermanentWidget(self.version_button)
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
        self.act_export.setIcon(icon("render", palette.accent().on_fill))
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
        self.publish_page.action_requested.connect(self._choose_and_upload)

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
            if self._page == RENDER:
                self._save_export_settings()
            if self._page == PUBLISH:
                self._save_upload_defaults()
            self.page_stack.setCurrentIndex(index)
            if index != EDIT:  # the editor with its video stays as it is
                animate_in(self.page_stack.currentWidget().widget())
            if index == PUBLISH:
                self._sync_publish_page()
                self._sync_analytics()
            if index == DATA:
                self._page = DATA
                self.thumbnails.refresh()
                self._update_youtube_preview()
                self._make_title_variants()
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
        elif self.ffmpeg is None:
            self.render_page.set_status("FFmpeg was not found: run tools\\setup_ffmpeg.py, then restart.", False)
        else:
            text = f"Length of the Short: <b>{format_timecode(timeline.output_us)}</b>"
            if rendered is not None:
                text += f" · <span style='color:{palette.SUCCESS}'>✓ exported</span>: {rendered}"
            elif self._rendered is not None:
                text += " · the edit changed since the last export: render again."
            self.render_page.set_status(text, True)
        if self._page == RENDER:
            self._sync_render_page()
        title = metadata.title.strip() or "<i>no title yet: fill it in on the Data page</i>"
        if rendered is not None and self._published == str(rendered):
            self.publish_page.set_status(f"<span style='color:{palette.SUCCESS}'>✓ Uploaded</span>: {rendered}")
        elif rendered is not None:
            self.publish_page.set_status(f"Ready to upload: {rendered}<br>Title: {title}")
        elif self.upload_widget is not None:
            self.publish_page.set_status(f"File: {self.upload_widget.file}<br>Title: {title}")
        else:
            self.publish_page.set_status("Export the video on the Render page first, or choose a file to upload.")

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

    # ------------------------------------------------------------ blocks
    def _block_groups(self) -> list[tuple[str, list]]:
        """The blocks (cards) the user can turn off, grouped by panel or page."""
        groups = []
        for dock in (self.properties_dock, self.subtitles_dock, self.transcript_dock, self.censor_dock,
                     self.blur_dock, self.highlights_dock, self.media_dock):
            cards = [c for c in dock.widget().findChildren(Card) if c.title_label.text()]
            if cards:
                groups.append((dock.windowTitle(), cards))
        groups.append(("Data page", [self.youtube_preview, self.thumbnail_editor]))
        groups.append(("Render page", [self.queue_panel]))
        groups.append(("Publish page", [self.analytics_panel]))
        return groups

    @staticmethod
    def _block_key(group: str, card) -> str:
        return f"{group}/{card.title_label.text()}"

    def _build_blocks_menu(self) -> None:
        """View → Blocks: a check per block; unticked blocks stay hidden, also after a restart."""
        menu = self.blocks_menu
        menu.clear()
        hidden = set(self.config.hidden_blocks)
        self.block_actions: dict[str, QAction] = {}
        for group, cards in self._block_groups():
            menu.addSection(group)
            for card in cards:
                key = self._block_key(group, card)
                action = QAction(card.title_label.text(), menu)
                action.setCheckable(True)
                action.setChecked(key not in hidden)
                action.toggled.connect(lambda on, k=key, c=card: self._set_block_shown(k, c, on))
                menu.addAction(action)
                self.block_actions[key] = action
                card.set_user_hidden(key in hidden)
                card.title_label.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
                card.title_label.customContextMenuRequested.connect(
                    lambda pos, c=card, a=action: self._block_context_menu(c, a, pos))
        menu.addSeparator()
        show_all = menu.addAction("Show all blocks")
        show_all.triggered.connect(self._show_all_blocks)

    def _set_block_shown(self, key: str, card, shown: bool) -> None:
        card.set_user_hidden(not shown)
        hidden = [k for k in self.config.hidden_blocks if k != key] + ([] if shown else [key])
        if hidden != self.config.hidden_blocks:
            self.config.hidden_blocks = hidden
            try:
                self.config.save()
            except OSError:
                pass

    def _block_context_menu(self, card, action: QAction, pos) -> None:
        """Right-click on a block's title: hide it (View → Blocks shows it again)."""
        menu = QMenu(self)
        hide = menu.addAction(f"Hide “{card.title_label.text()}”")
        hide.setToolTip("Bring it back with View → Blocks")
        if menu.exec(card.title_label.mapToGlobal(pos)) is hide:
            action.setChecked(False)

    def _show_all_blocks(self) -> None:
        for action in self.block_actions.values():
            action.setChecked(True)

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
        self.act_blur = QAction("Add blur", self, shortcut=QKeySequence(Qt.Key.Key_B))
        self.act_shortcuts = QAction("&Keyboard shortcuts", self, shortcut=QKeySequence(Qt.Key.Key_F1))
        self.act_shortcuts.triggered.connect(self.show_shortcuts)
        self.addAction(self.act_shortcuts)
        self.act_blur.setToolTip("Add a blur box at the playhead or over the selected range (B)")
        self._edit_actions = (
            self.act_undo, self.act_redo, self.act_split, self.act_delete, self.act_trim,
            self.act_restore, self.act_mute, self.act_merge_mutes, self.act_blur,
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
        edit.addSeparator()
        edit.addAction(self.act_blur)
        for action, glyph, short in ((self.act_split, "split", "Split"), (self.act_delete, "remove", "Delete"),
                                     (self.act_trim, "trim", "Trim"), (self.act_mute, "volume_off", "Mute"),
                                     (self.act_blur, "blur", "Blur")):
            action.setIcon(icon(glyph))
            action.setIconText(short)
        # Undo / redo live in the main command bar only.
        self.timeline_panel.add_actions([self.act_split, self.act_delete, self.act_trim, None, self.act_mute,
                                         self.act_blur])

        playback = self.menuBar().addMenu("&Playback")
        playback.addActions([self.act_play, self.act_prev, self.act_next])
        playback.addSeparator()
        playback.addActions([self.act_back_second, self.act_forward_second, self.act_go_start, self.act_go_end])

        view = self.menuBar().addMenu("&View")
        for dock in (self.media_dock, self.properties_dock, self.transcript_dock, self.highlights_dock, self.subtitles_dock,
                     self.censor_dock, self.timeline_dock):
            view.addAction(dock.toggleViewAction())
        self.blocks_menu = view.addMenu("Blocks")
        self.blocks_menu.setToolTipsVisible(True)
        self._build_blocks_menu()
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

        help_menu = self.help_menu = self.menuBar().addMenu("&Help")
        help_menu.addAction(self.act_shortcuts)
        help_menu.addSeparator()
        guides = help_menu.addMenu("Setup guides")
        for service, text in ((YOUTUBE, "YouTube"), (TIKTOK, "TikTok"), (INSTAGRAM, "Instagram Reels")):
            action = guides.addAction(text)
            action.triggered.connect(lambda _c=False, s=service: self.guide_panel.show_service(s))
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
        self.act_blur.triggered.connect(self.add_blur)
        self.blur_panel.seek_requested.connect(lambda us: self.preview.seek(us_to_ms(us)))
        self.blur_panel.edit_finished.connect(lambda: self.history.break_merge())
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
            self.config.transcribe_cross_check,
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
        self.subtitles_panel.set_user_presets(self.config.subtitle_presets)
        self.subtitles_panel.user_presets_changed.connect(self._subtitle_presets_changed)
        self.subtitles_panel.translate_requested.connect(self.translate_subtitles)
        self.subtitles_panel.translate_cancelled.connect(self.cancel_translation)
        self.subtitles_panel.edit_translation_requested.connect(self.edit_translation)
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
        self.censor_panel.set_defaults(CensorSettings.from_dict(self.config.censor_defaults))
        self.censor_panel.defaults_changed.connect(self._remember_censor_defaults)
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
            self._remember_folder("last_import_dir", Path(files[0]).parent)
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
        self._restore_clip_memory(info)
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

    def _remember_censor_defaults(self, settings: CensorSettings) -> None:
        """Padding, mode and categories stay for the next clip and the next start."""
        self.config.censor_defaults = settings.to_dict()
        self._save_config_quietly()

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
        """Ctrl+E: the Render page, where the export settings live."""
        media, timeline = self.preview.media, self.timeline.timeline
        if media is None or media.video is None or timeline is None or self.ffmpeg is None:
            return
        self.set_page(RENDER)

    def _sync_render_page(self) -> None:
        """Keep the export form on the Render page in step with the current clip and edit."""
        media, timeline = self.preview.media, self.timeline.timeline
        form = self.export_widget
        if media is None or media.video is None or timeline is None or self.ffmpeg is None:
            if form is not None and not form.busy:
                self._save_export_settings()
                self.render_page.set_content(None)
                self.export_widget = self._export_dialog = None
            return
        self._refresh_subtitles()
        style = self.subtitles_panel.style()
        if form is not None and not form.busy and Path(form.source.path) != Path(media.path):
            self._save_export_settings()
            self.render_page.set_content(None)
            form = None
        if form is None:
            composition = self.main_layout
            defaults = RenderSettings(width=composition.output_w, height=composition.output_h)
            settings = (RenderSettings.from_dict(self.config.export_settings) if self.config.export_settings
                        else defaults)
            output = default_output_path(Path(media.path), self.config.export_dir, settings.extension,
                                         self.metadata_panel.metadata().title)
            form = ExportDialog(media, composition, timeline, self.ffmpeg, settings, output, self.encoders, None,
                                upload=self.upload_to_youtube, captions=self.captions, subtitle_style=style)
            form.embed()
            form.queue_button.show()
            form.upload_check.show()
            form.upload_check.setChecked(bool(self.config.youtube_defaults.get("queue_upload", False)))
            form.upload_check.toggled.connect(self._remember_queue_upload)
            form.queued.connect(self._queue_render)
            form.rendered.connect(self._export_finished)
            form.folder_edit.editingFinished.connect(self._save_export_settings)
            form.browse_button.clicked.connect(self._save_export_settings)
            self.render_page.set_content(form)
            self.export_widget = self._export_dialog = form
        else:
            form.set_inputs(self.main_layout, timeline, self.captions, style)
            form.set_default_name(default_output_path(
                Path(media.path), form.folder_edit.text().strip(), form.settings().extension,
                self.metadata_panel.metadata().title).stem)

    def _save_export_settings(self) -> None:
        form = self.export_widget
        if form is not None:
            self.config.export_settings = form.settings().to_dict()
            self._update_subtitle_time(self.timeline.playhead_us)  # the preview shows the progress bar
            folder = form.folder_edit.text().strip()
            if folder and Path(folder).is_dir():
                self._remember_folder("export_dir", Path(folder))

    def _remember_queue_upload(self, on: bool) -> None:
        self.config.youtube_defaults = {**self.config.youtube_defaults, "queue_upload": bool(on)}
        try:
            self.config.save()
        except OSError:
            pass

    def _queue_render(self, job) -> None:
        form = self.export_widget
        if form is not None and form.upload_check.isChecked():
            plan = self._upload_plan()
            if plan is None:
                return  # told why; nothing queued
            job.upload = plan
        job.tag = self._render_state()
        self._save_export_settings()
        self.render_queue.add(job)

    def _upload_plan(self) -> UploadPlan | None:
        """Everything a queued upload needs, taken now: the Data page's text and
        thumbnail, the active channel, the last Publish page choices."""
        channel = self.youtube.active
        if channel is None or not self.youtube.has_token(channel.id):
            QMessageBox.warning(self, "Upload from the queue",
                                "Connect a YouTube channel first: Publish page → Accounts…")
            return None
        defaults = self.config.youtube_defaults
        meta = self.project.metadata
        category = defaults.get("category") or (
            meta.generated.category_id if meta.generated is not None and meta.generated.category_id
            else DEFAULT_CATEGORY)
        video = VideoMetadata(
            title=meta.title.strip(),
            description=meta.description,
            tags=tuple(meta.tags),
            category_id=str(category),
            privacy=str(defaults.get("privacy") or PRIVATE),
            language=str(defaults.get("language") or ""),
        )
        problems = video.problems()
        if problems:
            QMessageBox.warning(self, "Upload from the queue",
                                "Fill in the Data page first:\n\n" + "\n".join(problems))
            return None
        srt = ""
        if self.captions and defaults.get("captions", True):
            from core.subtitles.export import to_srt

            srt = to_srt(self.captions)
        return UploadPlan(
            channel.id, channel.title, video,
            captions_srt=srt,
            captions_language=self._captions_language(),
            thumbnail=self.thumbnails.job() if defaults.get("thumbnail", True) else None,
            client=self.youtube.client_for(channel.id),
        )

    def _queued_upload_done(self, job) -> None:
        plan, result = job.upload, job.upload.result
        self.upload_history.add(UploadRecord(
            uploaded_at=datetime.now().isoformat(timespec="seconds"),
            file=str(job.output),
            video_id=result.video_id,
            url=result.url,
            title=plan.metadata.title,
            channel_id=plan.channel_id,
            channel_title=plan.channel_title,
            privacy=result.privacy,
            publish_at=result.publish_at,
        ))
        self._analytics_loaded = ""
        source = getattr(job.source, "path", "")
        self._record_publication(cm.Publication(
            cm.YOUTUBE, result.url, plan.metadata.title, datetime.now().isoformat(timespec="seconds"),
            str(source) if source else "", result.video_id, plan.channel_title, result.privacy, str(job.output),
            variant=self._variant_text() if job.tag == self._render_state() else ""),
            plan.thumbnail)
        if job.tag == self._render_state():
            self._published = str(job.output)
        self.statusBar().showMessage(f"Uploaded to YouTube: {result.url}", 10000)
        self._refresh_steps()

    def _queued_render_done(self, job) -> None:
        if job.tag == self._render_state():  # still the current edit
            self._rendered = (Path(job.output), job.tag)
        self.statusBar().showMessage(f"Rendered {job.output}", 8000)
        self._refresh_steps()

    def _export_finished(self, path) -> None:
        self._save_export_settings()
        self._rendered = (Path(path), self._render_state())
        self.statusBar().showMessage(f"Exported {path}", 8000)
        self._refresh_steps()

    # -------------------------------------------------------------- youtube
    def manage_youtube_accounts(self) -> None:
        dialog = YouTubeAccountsDialog(self.youtube, self)
        dialog.exec()
        self.config.save()  # account list (no secrets) and the active channel

    def upload_to_youtube(self, path: Path | None = None) -> None:
        """The Publish page with the upload form for ``path`` (the last export when None)."""
        if path is None:
            path = self._rendered_path()
        if path is None:
            path = self._choose_upload_file()
            if path is None:
                return
        sources = {str(Path(m.path)).casefold() for m in self.project.media}
        if str(Path(path)).casefold() in sources:
            QMessageBox.warning(
                self, "Upload to YouTube",
                "This is a source clip of the project. Export the edited video first (Ctrl+E) and upload that.",
            )
            return
        self._show_upload_form(Path(path))
        self.set_page(PUBLISH)

    def _choose_upload_file(self) -> Path | None:
        start = self.config.export_dir or self.config.last_import_dir
        file, _ = QFileDialog.getOpenFileName(
            self, "Upload to YouTube", start, "Rendered video (*.mp4 *.mkv *.mov *.webm);;All files (*)"
        )
        return Path(file) if file else None

    def _choose_and_upload(self) -> None:
        path = self._choose_upload_file()
        if path is not None:
            self.upload_to_youtube(path)

    def _show_upload_form(self, path: Path, fresh: bool = False) -> None:
        form = self.upload_widget
        if form is not None and (form.busy or (form.file == path and not fresh)):
            return
        self._save_upload_defaults()
        form = UploadDialog(
            self.youtube, self.upload_history, path, self.ffmpeg.ffprobe if self.ffmpeg else None,
            self.config.youtube_defaults, self.manage_youtube_accounts, None,
            metadata=self.project.metadata,
            captions=self.captions,
            captions_language=self._captions_language(),
            thumbnail=self.thumbnails.job,
        )
        form.embed()
        form.uploaded.connect(lambda result, f=form: self._upload_finished(f, result))
        for combo in (form.category_combo, form.privacy_combo, form.language_combo):
            combo.currentIndexChanged.connect(lambda _i: self._save_upload_defaults())
        self.publish_page.set_content(form)
        self.upload_widget = form
        form.set_best_time(self._best_time, "" if self._analytics_loaded else
                           "Best time: connect YouTube; it is worked out from your channel's Shorts.")

    def _sync_publish_page(self) -> None:
        """Fill the upload form with the latest export and the Data page's metadata."""
        form = self.upload_widget
        if form is not None and (form.busy or form.result is not None):
            return  # uploading, or showing what was uploaded
        rendered = self._rendered_path()
        path = rendered or (form.file if form is not None else None)
        meta = self.metadata_panel.metadata()
        for panel in self.social_panels:
            if not panel.busy:
                panel.set_caption_source(meta.title, meta.description)
                panel.set_file(Path(path) if path is not None else None)
        if path is not None:
            self._show_upload_form(Path(path), fresh=True)

    # ------------------------------------------------------------ TikTok / Reels
    def _thumbnail_ms(self) -> int | None:
        """Where the cover frame is in the Short (ms), for the TikTok / Reels cover."""
        if not self.project.thumbnail.enabled:
            return None
        src = self.thumbnails.source()
        timeline = self.timeline.timeline
        if src is None or timeline is None:
            return None
        return timeline.source_to_output(src[1]) // 1000

    # ------------------------------------------------------- help
    def show_shortcuts(self) -> None:
        from ui.main_window.shortcuts_dialog import ShortcutsDialog, shortcut_rows

        if getattr(self, "shortcuts_dialog", None) is None:
            self.shortcuts_dialog = ShortcutsDialog(shortcut_rows(self), self)
        self.shortcuts_dialog.search_edit.clear()
        self.shortcuts_dialog.show()
        self.shortcuts_dialog.raise_()
        self.shortcuts_dialog.activateWindow()

    # ------------------------------------------------------- variants
    def _make_variants(self) -> None:
        self._make_title_variants()
        self._make_cover_variants()

    def _make_title_variants(self) -> None:
        from core.metadata.variants import title_variants

        meta = self.metadata_panel.metadata()
        try:
            variants = title_variants(self.metadata_panel.library, meta.selection)
        except Exception as exc:  # noqa: BLE001 - variants are a convenience; never block editing
            log.warning("Title variants: %s", exc)
            variants = []
        self.variants_card.set_titles(variants, meta.title)

    def _use_title_variant(self, variant) -> None:
        meta = self.metadata_panel.metadata().with_generated(variant.metadata)
        self.metadata_panel.set_metadata(meta)
        self._metadata_changed(self.metadata_panel.metadata())
        self.variants_card.mark_title(variant.title)

    def _make_cover_variants(self) -> None:
        from core.thumbnails.variants import cover_variants, render_variants

        frames = self.thumbnails.candidates
        design = self.project.thumbnail
        if not frames or not design.enabled:
            self.variants_card.set_covers([], [])
            return
        times = [us for us, _image in frames]
        variants = cover_variants(design, times, faces=self.thumbnails.faces_for(times))
        images = {us: image for us, image in frames}
        title = self.thumbnails.title()
        worker = Worker(render_variants, variants, images, title, 150, self.thumbnails.face_boxes(times))
        worker.signals.finished.connect(lambda result, w=worker, v=variants: self._cover_variants_done(w, v, result))
        worker.signals.failed.connect(lambda exc, w=worker: (self._pending.discard(w),
                                                             log.warning("Cover variants: %s", exc)))
        self._pending.add(worker)
        self._cover_variants_task = worker
        self.pool.start(worker)

    def _cover_variants_done(self, worker, variants, images) -> None:
        self._pending.discard(worker)
        if worker is self._cover_variants_task:
            self._cover_variants_task = None
            self.variants_card.set_covers(variants, images, self.project.thumbnail)

    def _use_cover_variant(self, variant) -> None:
        self.thumbnail_editor.set_design(variant.design)
        self.thumbnails._design_changed(variant.design)

    def _variant_text(self) -> str:
        """Which kind of title and cover the current Short has ("Streamer first · White on red")."""
        from core.metadata.variants import title_variants
        from core.thumbnails.model import STYLES

        meta = self.metadata_panel.metadata()
        label = ""
        try:
            for v in title_variants(self.metadata_panel.library, meta.selection):
                if v.title == meta.title:
                    label = v.label
                    break
        except Exception:  # noqa: BLE001
            pass
        design = self.project.thumbnail
        cover = design.text_style.name if design.enabled else "no cover"
        return " · ".join(part for part in (label or "own title", cover if design.style in STYLES else "") if part)

    # ------------------------------------------------------- clip memory
    def _current_source(self) -> str:
        media = self.preview.media
        return str(media.path) if media is not None else ""

    def _remember_clip(self) -> None:
        """Store the shown clip's title, description, tags and cover in the clip memory."""
        self._remember_timer.stop()
        source = self._memory_source
        if source and source == self._current_source():
            self.clip_memory.remember(source, self.project.metadata, self.project.thumbnail)

    def _restore_clip_memory(self, info) -> None:
        """Another clip is shown: bring back its title and cover, or clear the previous clip's."""
        if self._remember_timer.isActive():
            self._remember_clip()
        source = str(info.path) if info is not None else ""
        if not source or cm.clip_key(source) == cm.clip_key(self._memory_source or "\0"):
            self._memory_source = source
            self._show_clip_publications()
            return
        entry = self.clip_memory.get(source)
        previous = self.clip_memory.get(self._memory_source) if self._memory_source else None
        if entry is not None:
            metadata, thumbnail = entry.metadata, entry.thumbnail
        elif previous is not None and self.project.metadata == previous.metadata:
            metadata, thumbnail = cm.ClipMetadata(), cm.ThumbnailDesign()  # those were the other clip's
        else:
            metadata, thumbnail = self.project.metadata, self.project.thumbnail
        if metadata != self.project.metadata:
            self.project.metadata = metadata
            self.metadata_panel.set_metadata(metadata)
        if thumbnail != self.project.thumbnail:
            self.project.thumbnail = thumbnail
            self.thumbnail_editor.set_design(thumbnail)
        self._memory_source = source
        if entry is None:
            self.clip_memory.remember(source, metadata, thumbnail)  # a project's title for this clip
        self._show_clip_publications()

    def _show_clip_publications(self) -> None:
        source = self._memory_source
        items = self.clip_memory.publications(source) if source else []
        lines = []
        for p in items[:4]:
            link = (f" · <a style='color:{palette.accent().text}' href='{html.escape(p.url)}'>open</a>"
                    if p.url else "")
            lines.append(f"<b>{cm.SERVICE_TEXT.get(p.service, p.service)}</b> {p.date_text} as "
                         f"“{html.escape(p.title)}”{link}")
        if len(items) > 4:
            lines.append(f"and {len(items) - 4} more on the Publish page (History)")
        self.clip_published_label.setText(
            f"<span style='color:{palette.SUCCESS}'>✓ Published</span><br>" + "<br>".join(lines) if lines else "")
        self.clip_published_label.setVisible(bool(lines))

    def _record_publication(self, publication, cover_job=None) -> None:
        publication = self.clip_memory.add_publication(publication)
        if publication.source and publication.source == self._current_source():
            self.clip_memory.remember(publication.source, self.project.metadata, self.project.thumbnail)
        self.published_panel.refresh()
        self._show_clip_publications()
        if callable(cover_job):
            worker = Worker(cover_job)
            worker.signals.finished.connect(lambda data, p=publication, w=worker: self._cover_made(w, p, data))
            worker.signals.failed.connect(lambda exc, w=worker: (self._pending.discard(w),
                                                                 log.info("Cover for history: %s", exc)))
            self._pending.add(worker)
            self.pool.start(worker)

    def _cover_made(self, worker, publication, data) -> None:
        self._pending.discard(worker)
        if isinstance(data, (bytes, bytearray)) and data:
            self.clip_memory.set_cover(publication, bytes(data))
            self.published_panel.refresh()

    def _social_posted(self, results) -> None:
        meta = self.metadata_panel.metadata()
        for r in results:
            if not r.ok:
                continue
            service = cm.TIKTOK if r.service == TIKTOK else cm.INSTAGRAM
            panel = self.tiktok_panel if service == cm.TIKTOK else self.reels_panel
            account = self.social.tiktok_name if service == cm.TIKTOK else self.social.instagram_name
            self._record_publication(cm.Publication(
                service, r.url, meta.title or panel.caption_edit.toPlainText().strip(),
                datetime.now().isoformat(timespec="seconds"), self._current_source(), "", account or "", "",
                str(panel.file or ""), variant=self._variant_text()), self.thumbnails.job())

    def _open_published_clip(self, source: str) -> None:
        media = self.preview.media
        if media is not None and cm.clip_key(media.path) == cm.clip_key(source):
            self.set_page(DATA)
            return
        self.import_files([source])
        self.set_page(DATA)

    def _migrate_clip_memory(self) -> None:
        """First start with the clip memory: read in the old upload log and the saved projects."""
        memory = self.clip_memory
        if memory.migrated:
            return
        memory.import_upload_log(self.upload_history.records())
        folders = [Path(f) for f in (self.config.project_dir,) if f]
        seen = 0
        for folder in folders:
            try:
                files = sorted(folder.glob(f"*{PROJECT_EXTENSION}")) + sorted(folder.glob(f"*/*{PROJECT_EXTENSION}"))
            except OSError:
                continue
            for file in files[:300]:
                try:
                    seen += memory.import_project(load_project(file))
                except Exception as exc:  # noqa: BLE001 - an old or broken project is skipped
                    log.info("Clip memory: skipped %s (%s)", file, exc)
        memory.migrated = True
        memory.save()
        self.published_panel.refresh()
        log.info("Clip memory: %d uploads, %d clips from projects", len(memory.publications()), seen)

    @property
    def social_panels(self) -> tuple[CrossPostPanel, CrossPostPanel]:
        return self.tiktok_panel, self.reels_panel

    def _social_connections_changed(self) -> None:
        self._save_config_quietly()
        for panel in self.social_panels:
            panel.refresh_accounts()

    def _save_config_quietly(self) -> None:
        try:
            self.config.save()
        except OSError:
            pass

    def _save_upload_defaults(self) -> None:
        if self.upload_widget is not None:
            self.config.youtube_defaults = self.upload_widget.defaults()

    # ------------------------------------------------------------ analytics
    def _sync_analytics(self) -> None:
        """Show the active channel; load its numbers the first time the page opens."""
        channel = self.youtube.active
        connected = channel is not None and self.youtube.has_token(channel.id)
        self.analytics_panel.set_channel(channel.title if connected else "")
        if connected and self._analytics_loaded != channel.id and self._analytics_task is None:
            self.refresh_analytics()

    def refresh_analytics(self) -> None:
        from core.youtube.analytics import collect

        channel = self.youtube.active
        if channel is None or not self.youtube.has_token(channel.id) or self._analytics_task is not None:
            return
        app_ids = [r.video_id for r in self.upload_history.records() if r.channel_id == channel.id and r.video_id]
        app_ids += [v for v in self.clip_memory.video_ids() if v not in app_ids]  # the History tab's too
        task = CancellableTask(collect, self.youtube.client_for(channel.id), app_ids)
        task.signals.progress.connect(lambda _f, message: self.analytics_panel.set_busy(True, message))
        task.signals.finished.connect(lambda result, cid=channel.id: self._analytics_done(cid, result))
        task.signals.failed.connect(self._analytics_failed)
        task.signals.cancelled.connect(lambda: self._analytics_failed(None))
        self._analytics_task = task
        self._analytics_loaded = channel.id
        self.analytics_panel.set_busy(True, "Loading…")
        self.published_panel.set_busy(True)
        QThreadPool.globalInstance().start(task)

    def _refresh_published_numbers(self) -> None:
        channel = self.youtube.active
        if channel is None or not self.youtube.has_token(channel.id):
            self.published_panel.set_note("Connect a YouTube channel (YouTube tab → Accounts…) to read the numbers.")
            return
        self.refresh_analytics()

    def _analytics_done(self, channel_id: str, result) -> None:
        self._analytics_task = None
        stats, note = result
        self.analytics_panel.set_stats(stats, note)
        self.clip_memory.set_stats(stats)
        from core.youtube.best_time import best_time

        self._best_time = best_time(stats)
        if self.upload_widget is not None:
            self.upload_widget.set_best_time(self._best_time)
        self.published_panel.set_busy(False)
        self.published_panel.refresh()

    def _analytics_failed(self, error) -> None:
        self._analytics_task = None
        self._analytics_loaded = ""
        message = str(error) if error is not None else "Cancelled."
        self.analytics_panel.set_error(f"Could not load the analytics: {message}")
        self.published_panel.set_note(f"Could not read the numbers: {message}")

    def _upload_finished(self, form, result) -> None:
        self._save_upload_defaults()
        channel = self.youtube.active
        meta = form.metadata() if hasattr(form, "metadata") else self.metadata_panel.metadata()
        self._record_publication(cm.Publication(
            cm.YOUTUBE, result.url, meta.title, datetime.now().isoformat(timespec="seconds"),
            self._current_source(), result.video_id, channel.title if channel else "", result.privacy,
            str(form.file), variant=self._variant_text()), self.thumbnails.job())
        self._published = str(form.file)
        self.statusBar().showMessage(f"Uploaded to YouTube: {result.url}", 10000)
        self._analytics_loaded = ""  # the new video shows on the next visit
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
            old = self.project.metadata
            self.project.metadata = metadata
            self._set_dirty(True)
            self._remember_timer.start()
            if old.selection != metadata.selection or not self.variants_card.title_texts():
                self._make_title_variants()
            self.variants_card.mark_title(metadata.title)
        if self._page == DATA:
            self.thumbnails.refresh()  # the default text is the title
        self._update_youtube_preview()

    def _update_youtube_preview(self) -> None:
        if self._page != DATA:
            return  # updated when the Data page is shown
        meta = self.metadata_panel.metadata()
        channel = self.youtube.active
        media = self.preview.media
        frame = self.thumbnails.pixmap()
        if frame is None:
            frame = self.media_panel.thumbnail(media.path) if media is not None else None
        self.youtube_preview.set_metadata(meta.title, meta.description, meta.tags,
                                          channel.title if channel else "", frame)

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
        self.config.transcribe_cross_check = quality["cross_check"]

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
        self.act_blur.setEnabled(has)
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
        self.thumbnail_editor.set_design(self.project.thumbnail)
        self.media_panel.clear()
        self.preview.unload()
        self.properties_panel.show_media(None)
        self._apply_project_composition()
        self._set_dirty(False)

    def _project_folder(self) -> str:
        """Where Open/Save Project start: the last project folder, else the last import folder."""
        for folder in (self.config.project_dir, self.config.last_import_dir):
            if folder and Path(folder).is_dir():
                return folder
        return ""

    def _remember_folder(self, key: str, folder: Path) -> None:
        """Keep a last-used folder and write the settings at once (not only on exit)."""
        if str(folder) and getattr(self.config, key) != str(folder):
            setattr(self.config, key, str(folder))
            try:
                self.config.save()
            except OSError:
                pass

    def choose_project(self) -> None:
        if not self._confirm_discard():
            return
        file, _ = QFileDialog.getOpenFileName(
            self, "Open Project", self._project_folder(), f"Project (*{PROJECT_EXTENSION})"
        )
        if file:
            self.open_project(Path(file))

    def open_project(self, path: Path) -> None:
        try:
            project = load_project(path)
        except (OSError, ProjectFormatError) as exc:
            QMessageBox.warning(self, "Cannot open project", f"{path.name}\n\n{exc}")
            return
        self._remember_folder("project_dir", Path(path).parent)
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
        self.thumbnail_editor.set_design(self.project.thumbnail)
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
        start = Path(self._project_folder()) / f"{self.project.name}{PROJECT_EXTENSION}"
        file, _ = QFileDialog.getSaveFileName(self, "Save Project", str(start), f"Project (*{PROJECT_EXTENSION})")
        if not file:
            return False
        self.project_path = Path(file)
        self._remember_folder("project_dir", self.project_path.parent)
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

    def _subtitle_presets_changed(self, presets: dict) -> None:
        self.config.subtitle_presets = presets
        try:
            self.config.save()
        except OSError:
            pass

    def current_translation(self):
        media = self.preview.media
        return self.project.translation_for(str(media.path)) if media is not None else None

    def caption_transcript(self):
        """The transcript the captions are made from: the translation when it is switched on."""
        translation = self.current_translation()
        if translation is not None and self.subtitles_panel.settings().translated:
            return translation
        return self.transcript.transcript

    def _captions_language(self) -> str:
        return getattr(self.caption_transcript(), "language", "") or ""

    def translate_subtitles(self, direction: str = "") -> bool:
        """Translate the transcript into the other language (model downloaded on first use)."""
        from core.translation.models import direction_for
        from core.translation.transcript import translate_file

        media, t = self.preview.media, self.transcript.transcript
        if media is None or t is None or self._translate_task is not None:
            return False
        direction = direction or direction_for(t.language)
        task = CancellableTask(translate_file, self.engine.models_dir, t, direction)
        task.signals.progress.connect(self.subtitles_panel.set_translation_progress)
        task.signals.finished.connect(lambda result, p=str(media.path): self._translation_done(p, result))
        task.signals.failed.connect(self._translation_failed)
        task.signals.cancelled.connect(lambda: self._translation_failed(None))
        self._translate_task = task
        self.subtitles_panel.set_translation_busy(True, "Preparing the translation…")
        log.info("Translating subtitles of %s (%s)", media.path, direction)
        self.pool.start(task)
        return True

    def cancel_translation(self) -> None:
        if self._translate_task is not None:
            self._translate_task.cancel()

    def _translation_done(self, path: str, translation) -> None:
        self._translate_task = None
        self.project.set_translation(path, translation)
        self._set_dirty(True)
        settings = self.subtitles_panel.settings()
        if not settings.translated:  # show what was just made
            self.subtitles_panel.set_settings(replace(settings, translated=True))
            self.project.subtitles = self.subtitles_panel.settings()
        self.subtitles_panel.set_translation_busy(False)
        self._refresh_subtitles()
        self.statusBar().showMessage("Subtitles translated. Untick «Show the translation» for the original.", 8000)

    def _translation_failed(self, exc) -> None:
        self._translate_task = None
        if exc is None or type(exc).__name__.endswith("Cancelled"):
            message = "Translation cancelled."
        else:
            log.warning("Translation failed: %s", exc)
            message = f"Translation failed: {exc}"
        self.subtitles_panel.set_translation_busy(False)
        self._refresh_subtitles()
        self.subtitles_panel.set_translation_busy(False, message)

    def edit_translation(self) -> bool:
        from ui.subtitles.translation_dialog import TranslationDialog

        media, original, translation = self.preview.media, self.transcript.transcript, self.current_translation()
        if media is None or original is None or translation is None:
            return False
        dialog = TranslationDialog(original, translation, self)
        if dialog.exec() != TranslationDialog.DialogCode.Accepted or not dialog.changes():
            return False
        self.project.set_translation(str(media.path), dialog.edited())
        self._set_dirty(True)
        self._refresh_subtitles()
        return True

    def _refresh_subtitles(self) -> None:
        settings = self.subtitles_panel.settings()
        timeline = self.timeline.timeline
        transcript = self.caption_transcript()
        self.subtitles_panel.set_has_transcript(self.transcript.transcript is not None)
        if self._translate_task is None:
            self.subtitles_panel.set_translation(self.current_translation(),
                                                 getattr(self.transcript.transcript, "language", "") or "")
        self.captions = build_captions(transcript, timeline, settings.style, settings.edits)
        self.subtitles_panel.set_captions(self.captions)
        self.preview.output_view.set_subtitles(self.captions, settings.style)
        self._update_subtitle_time(self.timeline.playhead_us)

    def _update_subtitle_time(self, source_us: int) -> None:
        timeline = self.timeline.timeline
        out = timeline.source_to_output(source_us) if timeline is not None else None
        settings = self.config.export_settings or {}
        self.preview.output_view.set_progress_bar(settings.get("progress_bar", ""), settings.get("progress_color", ""),
                                                  timeline.output_us if timeline is not None else 0)
        self.preview.output_view.set_subtitle_time(out)

    def add_blur(self) -> bool:
        """Edit → Add blur (B): a new box, with the Blur panel shown."""
        if not self.timeline.add_blur():
            return False
        self.blur_dock.show()
        self.blur_dock.raise_()
        self.statusBar().showMessage("Blur added: drag the teal box over what to hide", 5000)
        return True

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
        running = [name for name, form in (("rendering", self.export_widget), ("uploading", self.upload_widget),
                                           ("rendering the queue", self.render_queue),
                                           ("posting to TikTok", self.tiktok_panel),
                                           ("posting to Instagram", self.reels_panel))
                   if form is not None and form.busy]
        if running:
            answer = QMessageBox.question(self, APP_NAME, f"Still {' and '.join(running)}. Stop and quit?")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        if not self._confirm_discard():
            event.ignore()
            return
        for form, stop in ((self.export_widget, "cancel_render"), (self.upload_widget, "cancel_upload"),
                           (self.render_queue, "stop")):
            if form is not None and form.busy:
                getattr(form, stop)()
        self._save_export_settings()
        self._save_upload_defaults()
        if self._transcribe_task is not None:
            self._transcribe_task.cancel()
        if self._translate_task is not None:
            self._translate_task.cancel()
        self.edit_playback.shutdown()
        self.listen.stop()
        self.autoframe.cancel()
        self.blur_runner.cancel()
        if self._analytics_task is not None:
            self._analytics_task.cancel()
        for panel in self.social_panels:
            panel.cancel()
        if self._remember_timer.isActive():
            self._remember_clip()
        self.guide_panel.tiktok_setup.cancel()
        self.updates.shutdown()
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
        from ui.main_window.about_dialog import AboutDialog

        self.about_dialog = AboutDialog(self)
        self.about_dialog.open()

    def _show_ffmpeg_info(self) -> None:
        if self.ffmpeg is None:
            text = "FFmpeg was not found."
        else:
            text = (
                f"ffmpeg: {self.ffmpeg.ffmpeg}\n{tool_version(self.ffmpeg.ffmpeg)}\n\n"
                f"ffprobe: {self.ffmpeg.ffprobe}\n{tool_version(self.ffmpeg.ffprobe)}"
            )
        QMessageBox.information(self, "FFmpeg", text)


def _queued_upload(job, progress=None, cancel=None):
    """Upload a rendered queue job (runs off the GUI thread)."""
    plan = job.upload
    return _upload(plan.client, Path(job.output), plan.metadata, progress, cancel,
                   captions_srt=plan.captions_srt, captions_language=plan.captions_language,
                   thumbnail=plan.thumbnail)
