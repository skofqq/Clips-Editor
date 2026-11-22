"""Learning which tags and hashtags go with more views on the channel.

Every refresh of the YouTube numbers gives each Short's views, watch share,
tags and the #hashtags in its title and description. A tag's effect is how
far above (or below) the channel's usual views its Shorts do, measured on a
log scale so one viral video does not decide everything, and pulled towards
"no effect" while a tag has few videos. Shorts younger than two days are
left out because their views are still growing.

The result is saved per channel, so suggestions work at once on the next
start and grow better with every new Short.
"""

from __future__ import annotations

import json
import logging
import math
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

MIN_VIDEOS = 6  # Shorts needed before suggesting anything
MIN_TAG_VIDEOS = 2
SETTLE_DAYS = 2
PRIOR = 2.0  # pseudo-videos of "no effect": a tag needs several Shorts to stand out
GOOD_LIFT = 1.15
POOR_LIFT = 0.85
TAG, HASHTAG = "tag", "hashtag"


@dataclass(frozen=True)
class TagScore:
    text: str  # as written most recently, without "#"
    kind: str  # TAG or HASHTAG
    videos: int
    lift: float  # 1.6 = Shorts with it get ~1.6× the channel's usual views
    median_views: int
    watched_pct: float | None = None  # average share watched of its Shorts
    partners: tuple[str, ...] = ()  # casefolded tags/hashtags it was used together with

    @property
    def key(self) -> str:
        return self.text.casefold()

    @property
    def label(self) -> str:
        return ("#" if self.kind == HASHTAG else "") + self.text


@dataclass(frozen=True)
class TagInsights:
    channel: str = ""
    videos: int = 0  # Shorts learned from
    median_views: int = 0
    learned_at: str = ""
    scores: tuple[TagScore, ...] = ()

    @property
    def ready(self) -> bool:
        return self.videos >= MIN_VIDEOS and bool(self.scores)

    def score(self, text: str, kind: str) -> TagScore | None:
        key = text.lstrip("#").casefold()
        return next((s for s in self.scores if s.kind == kind and s.key == key), None)

    def to_dict(self) -> dict:
        return {"channel": self.channel, "videos": self.videos, "median_views": self.median_views,
                "learned_at": self.learned_at,
                "scores": [{**asdict(s), "partners": list(s.partners)} for s in self.scores]}

    @classmethod
    def from_dict(cls, data) -> "TagInsights":
        if not isinstance(data, dict):
            return cls()
        scores = []
        for raw in data.get("scores") or []:
            try:
                scores.append(TagScore(str(raw["text"]), str(raw["kind"]), int(raw["videos"]), float(raw["lift"]),
                                       int(raw["median_views"]),
                                       None if raw.get("watched_pct") is None else float(raw["watched_pct"]),
                                       tuple(str(p) for p in raw.get("partners") or ())))
            except (KeyError, TypeError, ValueError):
                continue
        return cls(str(data.get("channel") or ""), int(data.get("videos") or 0),
                   int(data.get("median_views") or 0), str(data.get("learned_at") or ""), tuple(scores))


def _published(text: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat((text or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def learn(stats, channel: str = "", now: datetime | None = None) -> TagInsights:
    """Tag effects from the channel's video stats (core.youtube.analytics.VideoStats)."""
    now = now or datetime.now(timezone.utc)
    settled = []
    for s in stats:
        published = _published(s.published_at)
        if not s.is_short or published is None or now - published < timedelta(days=SETTLE_DAYS):
            continue
        if s.privacy and s.privacy != "public":
            continue
        settled.append(s)
    stamp = now.isoformat(timespec="seconds")
    if len(settled) < MIN_VIDEOS:
        return TagInsights(channel, len(settled), 0, stamp, ())
    logs = [math.log1p(s.views) for s in settled]
    mean = statistics.fmean(logs)
    usual = int(statistics.median(s.views for s in settled))

    groups: dict[tuple[str, str], list] = {}
    names: dict[tuple[str, str], tuple[str, str]] = {}  # key → (published, text) of the newest use
    for s, value in zip(settled, logs):
        labels = {(TAG, t.casefold()): t for t in s.tags} | {(HASHTAG, h.casefold()): h for h in s.hashtags}
        for key, text in labels.items():
            groups.setdefault(key, []).append((s, value, labels))
            if key not in names or s.published_at > names[key][0]:
                names[key] = (s.published_at, text)

    scores = []
    for key, items in groups.items():
        n = len(items)
        if n < MIN_TAG_VIDEOS or n >= len(settled) * 0.9:  # too rare to tell, or on (almost) every Short
            continue
        effect = sum(value - mean for _s, value, _l in items) / (n + PRIOR)
        shares = [s.avg_view_pct for s, _v, _l in items if s.avg_view_pct is not None]
        partners: dict[str, int] = {}
        for _s, _v, labels in items:
            for other in labels:
                if other != key:
                    partners[other[1]] = partners.get(other[1], 0) + 1
        scores.append(TagScore(
            names[key][1], key[0], n, round(math.exp(effect), 3),
            int(statistics.median(s.views for s, _v, _l in items)),
            round(statistics.fmean(shares), 1) if shares else None,
            tuple(sorted(partners, key=lambda p: -partners[p])[:20]),
        ))
    scores.sort(key=lambda s: -s.lift)
    return TagInsights(channel, len(settled), usual, stamp, tuple(scores))


@dataclass(frozen=True)
class Suggestion:
    score: TagScore
    related: bool  # used before together with this clip's tags


def suggest(insights: TagInsights, tags, hashtags, limit: int = 8) -> list[Suggestion]:
    """Tags and hashtags that did well and this clip does not have yet. Ones that went with
    the clip's own tags (same streamer, game…) come first."""
    if not insights.ready:
        return []
    have_tags = {t.lstrip("#").casefold() for t in tags}
    have_hash = {h.lstrip("#").casefold() for h in hashtags}
    have = have_tags | have_hash
    found = []
    for s in insights.scores:
        if s.lift < GOOD_LIFT or (s.kind == TAG and s.key in have_tags) or (s.kind == HASHTAG and s.key in have_hash):
            continue
        related = bool(have & set(s.partners))
        found.append(Suggestion(s, related))
    found.sort(key=lambda g: -(math.log(g.score.lift) * (1.5 if g.related else 1.0)))
    return found[:limit]


def weak(insights: TagInsights, tags, hashtags) -> list[TagScore]:
    """The clip's own tags whose Shorts did clearly worse than usual."""
    if not insights.ready:
        return []
    result = []
    for text in tags:
        s = insights.score(text, TAG)
        if s is not None and s.lift <= POOR_LIFT:
            result.append(s)
    for text in hashtags:
        s = insights.score(text, HASHTAG)
        if s is not None and s.lift <= POOR_LIFT:
            result.append(s)
    return result


@dataclass
class TagInsightsStore:
    """data_dir/tag_insights.json: the latest insights per channel id."""

    path: Path
    channels: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "TagInsightsStore":
        store = cls(Path(path))
        try:
            raw = json.loads(store.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return store
        except (OSError, ValueError) as exc:
            log.warning("Could not read %s: %s", path, exc)
            return store
        if isinstance(raw, dict):
            store.channels = {str(k): TagInsights.from_dict(v) for k, v in raw.items()}
        return store

    def get(self, channel: str) -> TagInsights:
        return self.channels.get(channel) or TagInsights(channel)

    def put(self, insights: TagInsights) -> None:
        self.channels[insights.channel] = insights
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({k: v.to_dict() for k, v in self.channels.items()}, ensure_ascii=False,
                                      indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            log.warning("Could not save %s: %s", self.path, exc)
