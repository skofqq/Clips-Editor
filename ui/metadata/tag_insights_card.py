""""Tags that bring views": what the app learned from the channel's Shorts, as
clickable chips. A hashtag goes into the description, a tag into the tags."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QLabel, QPushButton, QWidget

from core.metadata.tag_insights import HASHTAG, MIN_VIDEOS, TagInsights, TagScore, suggest, weak
from ui.chips.chip_input import FlowLayout
from ui.widgets.card import Card


def _views(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.1f}K"
    return str(n)


def describe(score: TagScore, usual: int) -> str:
    text = (f"{score.label}: Shorts with it get ×{score.lift:.2f} of your usual views "
            f"(median {_views(score.median_views)} vs {_views(usual)}, {score.videos} Shorts)")
    if score.watched_pct is not None:
        text += f", watched {score.watched_pct:.0f} % on average"
    return text


class TagInsightsCard(Card):
    hashtag_chosen = Signal(str)  # without "#"
    tag_chosen = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Tags that bring views", "Learned from your channel's Shorts. Click one to add it.",
                         parent)
        self._insights = TagInsights()
        self.status = QLabel(self)
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        self.hashtags_label = QLabel("For the description", self)
        self.tags_label = QLabel("Tags", self)
        self.hashtag_box, self.tag_box = QWidget(self), QWidget(self)
        for box in (self.hashtag_box, self.tag_box):
            box.setStyleSheet("background: transparent;")
        self.hashtag_flow, self.tag_flow = FlowLayout(self.hashtag_box, 6), FlowLayout(self.tag_box, 6)
        self.weak_label = QLabel(self)
        self.weak_label.setObjectName("muted")
        self.weak_label.setWordWrap(True)
        for widget in (self.status, self.hashtags_label, self.hashtag_box, self.tags_label, self.tag_box,
                       self.weak_label):
            self.body.addWidget(widget)
        self.buttons: list[QPushButton] = []
        self.update_for((), ())

    def set_insights(self, insights: TagInsights | None) -> None:
        self._insights = insights or TagInsights()

    def insights(self) -> TagInsights:
        return self._insights

    def update_for(self, tags, hashtags) -> None:
        """Show what this clip could add, given its current tags and description hashtags."""
        for button in self.buttons:
            button.hide()
            button.deleteLater()
        self.buttons = []
        for flow in (self.hashtag_flow, self.tag_flow):
            while flow.count():
                flow.takeAt(0)
        insights = self._insights
        found = suggest(insights, tags, hashtags)
        if not insights.ready:
            self.status.setText(
                f"Learning starts when the YouTube numbers load (Publish page) and needs {MIN_VIDEOS} public "
                f"Shorts older than two days; {insights.videos} so far." if insights.videos else
                "Connect your channel and open the Publish page: the app reads how your Shorts did and learns "
                "which tags and hashtags go with more views.")
        elif not found:
            self.status.setText(f"Learned from {insights.videos} Shorts. Nothing better to add: this clip "
                                "already has the tags that work.")
        else:
            self.status.setText(f"Learned from {insights.videos} Shorts (usual views {_views(insights.median_views)}).")
        for suggestion in found:
            s = suggestion.score
            button = QPushButton(f"+ {s.label}  ×{s.lift:.1f}", self)
            button.setObjectName("outlined" if not suggestion.related else "")
            button.setToolTip(describe(s, insights.median_views)
                              + ("\nUsed before with this clip's tags." if suggestion.related else ""))
            if s.kind == HASHTAG:
                button.clicked.connect(lambda _c=False, t=s.text: self.hashtag_chosen.emit(t))
                self.hashtag_flow.addWidget(button)
            else:
                button.clicked.connect(lambda _c=False, t=s.text: self.tag_chosen.emit(t))
                self.tag_flow.addWidget(button)
            self.buttons.append(button)
        has_hash = any(g.score.kind == HASHTAG for g in found)
        has_tag = any(g.score.kind != HASHTAG for g in found)
        self.hashtags_label.setVisible(has_hash)
        self.hashtag_box.setVisible(has_hash)
        self.tags_label.setVisible(has_tag)
        self.tag_box.setVisible(has_tag)
        poor = weak(insights, tags, hashtags)
        self.weak_label.setVisible(bool(poor))
        self.weak_label.setText("Below your usual views: " + ", ".join(f"{s.label} ×{s.lift:.1f}" for s in poor)
                                if poor else "")
        self.hashtag_box.updateGeometry()
        self.tag_box.updateGeometry()

    def suggestion_labels(self) -> list[str]:
        return [b.text() for b in self.buttons]
