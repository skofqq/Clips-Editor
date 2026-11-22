"""Learning which tags and hashtags bring views, and suggesting them on the Data page."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from core.metadata.tag_insights import (
    HASHTAG,
    MIN_VIDEOS,
    TAG,
    TagInsights,
    TagInsightsStore,
    learn,
    suggest,
    weak,
)
from core.youtube.analytics import VideoStats, find_hashtags, video_stats

NOW = datetime(2026, 11, 22, 12, tzinfo=timezone.utc)


def _video(i: int, views: int, tags=(), hashtags=(), days: int = 10, pct=None, length: int = 40) -> VideoStats:
    published = (NOW - timedelta(days=days)).isoformat().replace("+00:00", "Z")
    return VideoStats(f"v{i}", f"Clip {i}", published, length, views, 0, 0, "public", pct,
                      tags=tuple(tags), hashtags=tuple(hashtags))


def _channel() -> list[VideoStats]:
    videos = []
    for i in range(12):  # #sasavot everywhere; #funny Shorts do 4× better; "cs2" Shorts do worse
        funny = i % 3 == 0
        cs2 = i % 4 == 1
        views = 4000 if funny else (500 if cs2 else 1000)
        tags = ["sasavot", "twitch"] + (["cs2"] if cs2 else []) + (["clutch"] if funny else [])
        hashtags = ["sasavot", "shorts"] + (["funny"] if funny else [])
        videos.append(_video(i, views + i, tags, hashtags, pct=80.0 if funny else 60.0))
    return videos


def test_find_hashtags():
    assert find_hashtags("Ахаха | #sasavot on #Twitch\n#shorts #Shorts #2024 &#39; a#b") == ("sasavot", "Twitch", "shorts")
    assert find_hashtags("") == ()


def test_video_stats_reads_tags_and_hashtags():
    class Client:
        def get_json(self, url):
            return {"items": [{"id": "a", "snippet": {"title": "Клатч #cs2", "publishedAt": "2026-11-01T10:00:00Z",
                                                      "description": "#sasavot #shorts\nlink", "tags": ["sasavot", " "]},
                               "statistics": {"viewCount": "10"}, "contentDetails": {"duration": "PT30S"}}]}

    [stats] = video_stats(Client(), ["a"])
    assert stats.tags == ("sasavot",) and stats.hashtags == ("cs2", "sasavot", "shorts")


def test_learn_finds_what_works():
    insights = learn(_channel(), "UC1", NOW)
    assert insights.ready and insights.videos == 12 and insights.channel == "UC1"
    funny = insights.score("#funny", HASHTAG)
    assert funny is not None and funny.lift > 1.5 and funny.videos == 4 and funny.watched_pct == 80.0
    assert insights.score("clutch", TAG).lift > 1.5
    assert insights.score("cs2", TAG).lift < 0.85
    assert insights.score("sasavot", TAG) is None  # on every Short: says nothing
    assert insights.scores[0].lift >= insights.scores[-1].lift
    assert TagInsights.from_dict(insights.to_dict()) == insights


def test_learning_skips_fresh_long_and_private_videos():
    videos = _channel()[:MIN_VIDEOS - 1]
    extra = [_video(100, 9000, days=1), _video(101, 9000, length=900), replace(_video(102, 9000), privacy="private")]
    insights = learn(videos + extra, "UC1", NOW)
    assert not insights.ready and insights.videos == MIN_VIDEOS - 1
    assert suggest(insights, [], []) == [] and weak(insights, ["cs2"], []) == []


def test_suggest_and_weak():
    insights = learn(_channel(), "UC1", NOW)
    found = suggest(insights, ["sasavot"], ["sasavot"])
    labels = [g.score.label for g in found]
    assert "#funny" in labels and "clutch" in labels and "cs2" not in labels
    assert all(g.related for g in found)  # all went with #sasavot before
    assert "#funny" not in [g.score.label for g in suggest(insights, [], ["FUNNY"])]  # already there
    assert [s.label for s in weak(insights, ["cs2", "sasavot"], ["shorts"])] == ["cs2"]


def test_store(tmp_path):
    store = TagInsightsStore.load(tmp_path / "t.json")
    assert not store.get("UC1").ready
    store.put(learn(_channel(), "UC1", NOW))
    again = TagInsightsStore.load(tmp_path / "t.json")
    assert again.get("UC1").ready and not again.get("UC2").ready
    (tmp_path / "bad.json").write_text("{nope", encoding="utf-8")
    assert TagInsightsStore.load(tmp_path / "bad.json").channels == {}


def test_metadata_panel_suggests_and_adds(qapp, tmp_path):
    from core.metadata.generate import ClipMetadata
    from core.metadata.library import Library
    from ui.metadata.metadata_panel import MetadataPanel

    panel = MetadataPanel(Library(tmp_path / "lib.sqlite"))
    card = panel.insights_card
    assert not card.buttons and "Connect your channel" in card.status.text()
    panel.set_metadata(ClipMetadata(title="Клатч", description="Лучший момент\n#sasavot #shorts", tags=("sasavot", "cs2")))
    panel.set_tag_insights(learn(_channel(), "UC1", NOW))
    labels = card.suggestion_labels()
    assert any("#funny" in t for t in labels) and any("clutch" in t for t in labels)
    assert "cs2" in card.weak_label.text() and not card.weak_label.isHidden()
    seen = []
    panel.metadata_changed.connect(seen.append)
    card.hashtag_chosen.emit("funny")
    assert seen[-1].description == "Лучший момент\n#sasavot #shorts #funny"
    assert not any("#funny" in t for t in card.suggestion_labels())  # added: no longer suggested
    card.tag_chosen.emit("clutch")
    assert "clutch" in seen[-1].tags
    panel.add_hashtag("#funny")  # never twice
    assert seen[-1].description.count("#funny") == 1
    panel.set_metadata(ClipMetadata(description="Просто текст"))
    panel.add_hashtag("wow")
    assert panel.metadata().description == "Просто текст\n\n#wow"
    panel.deleteLater()
