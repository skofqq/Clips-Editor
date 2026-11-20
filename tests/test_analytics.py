"""Stage 10: Shorts analytics."""

from __future__ import annotations

import urllib.parse
from datetime import date

import pytest

from core.youtube import analytics
from core.youtube.analytics import VideoStats, collect, parse_duration, summarize
from core.youtube.errors import YouTubeError


class FakeClient:
    def __init__(self, analytics_error: YouTubeError | None = None) -> None:
        self.analytics_error = analytics_error
        self.urls: list[str] = []

    def get_json(self, url: str) -> dict:
        self.urls.append(url)
        parsed = urllib.parse.urlparse(url)
        q = dict(urllib.parse.parse_qsl(parsed.query))
        if parsed.path.endswith("/channels"):
            return {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]}
        if parsed.path.endswith("/playlistItems"):
            assert q["playlistId"] == "UU1"
            return {"items": [{"contentDetails": {"videoId": v}} for v in ("a", "b", "long")]}
        if parsed.path.endswith("/videos"):
            data = {
                "a": ("Охота", "2026-11-18T10:00:00Z", "PT45S", 1500, 120, 4),
                "b": ("Ржака", "2026-11-19T10:00:00Z", "PT1M5S", 300, 9, 0),
                "long": ("Стрим", "2026-11-01T10:00:00Z", "PT2H3M", 50, 2, 1),
                "old": ("Старое", "2026-10-01T10:00:00Z", "PT30S", 10, 0, 0),
            }
            return {"items": [{
                "id": v, "snippet": {"title": d[0], "publishedAt": d[1]}, "contentDetails": {"duration": d[2]},
                "statistics": {"viewCount": str(d[3]), "likeCount": str(d[4]), "commentCount": str(d[5])},
                "status": {"privacyStatus": "public"}} for v, d in data.items() if v in q["id"].split(",")]}
        if parsed.netloc == "youtubeanalytics.googleapis.com":
            if self.analytics_error is not None:
                raise self.analytics_error
            assert q["ids"] == "channel==MINE" and q["filters"].startswith("video==")
            assert q["startDate"] == "2026-10-01" and q["endDate"] == "2026-11-20"
            return {"columnHeaders": [{"name": n} for n in ("video", "averageViewPercentage",
                                                            "averageViewDuration", "subscribersGained")],
                    "rows": [["a", 82.5, 37.1, 6], ["b", 40.0, 26.0, 1]]}
        raise AssertionError(url)


def test_parse_duration():
    assert parse_duration("PT45S") == 45 and parse_duration("PT1M5S") == 65
    assert parse_duration("PT2H3M") == 7380 and parse_duration("P1DT1S") == 86401 and parse_duration("x") == 0


def test_collect_joins_counts_and_retention():
    stats, note = collect(FakeClient(), ["a", "old"], today=date(2026, 11, 20))
    assert note == ""
    assert [s.video_id for s in stats] == ["b", "a", "long", "old"]  # newest first
    by_id = {s.video_id: s for s in stats}
    assert by_id["a"].from_app and by_id["old"].from_app and not by_id["b"].from_app
    assert by_id["a"].avg_view_pct == 82.5 and by_id["a"].subscribers == 6 and by_id["a"].like_rate == 8.0
    assert by_id["long"].avg_view_pct is None and not by_id["long"].is_short and by_id["b"].is_short
    summary = summarize([s for s in stats if s.is_short])
    assert summary.videos == 3 and summary.views == 1810 and summary.best.video_id == "a"
    assert summary.avg_view_pct == pytest.approx((82.5 * 1500 + 40 * 300) / 1800)
    assert summary.subscribers == 7


@pytest.mark.parametrize("error, words", [
    (YouTubeError("YouTube error 403: API has not been used", 403, "accessNotConfigured"), "enable"),
    (YouTubeError("insufficient", 403, "insufficientPermissions"), "reconnect"),
])
def test_collect_without_analytics(error, words):
    stats, note = collect(FakeClient(error), [], today=date(2026, 11, 20))
    assert len(stats) == 3 and all(s.avg_view_pct is None for s in stats)
    assert words in note.lower()


def test_analytics_scope_is_requested():
    from core.youtube import oauth

    assert oauth.ANALYTICS_SCOPE in oauth.SCOPES and oauth.ANALYTICS_SCOPE not in oauth.REQUIRED_SCOPES


def test_panel_filters_and_sorts(qapp):
    from ui.youtube.analytics_panel import AnalyticsPanel

    stats = [
        VideoStats("a", "Охота", "2026-11-18T10:00:00Z", 45, 1500, 120, 4, avg_view_pct=82.5, subscribers=6,
                   from_app=True),
        VideoStats("b", "Ржака", "2026-11-19T10:00:00Z", 65, 300, 9, 0),
        VideoStats("long", "Стрим", "2026-11-01T10:00:00Z", 7380, 50_000, 2, 1),
    ]
    panel = AnalyticsPanel()
    panel.set_channel("Skof Clips")
    panel.set_stats(stats, "Watch time is not available: x")
    assert panel.table.rowCount() == 2  # only Shorts
    assert panel.tiles["views"].value.text() == "1.8K" and "Охота" in panel.best_label.text()
    assert "Watch time is not available" in panel.status.text()
    panel.table.sortItems(2)  # views, ascending
    assert panel.table.item(0, 0).text() == "Ржака"
    panel.app_check.setChecked(True)
    assert panel.table.rowCount() == 1 and panel.table.item(0, 0).text() == "● Охота"
    panel.shorts_check.setChecked(False)
    panel.app_check.setChecked(False)
    assert panel.table.rowCount() == 3 and panel.tiles["views"].value.text() == "52K"


def test_publish_page_loads_analytics(qapp, data_dir, ffmpeg_paths, monkeypatch):
    from core.youtube.api import Channel
    from tests.test_steps import _wait, _window
    from ui.main_window.pages import PUBLISH

    calls = []

    def fake_collect(client, app_ids, progress=None, cancel=None):
        calls.append(app_ids)
        return [VideoStats("a", "Охота", "2026-11-18T10:00:00Z", 45, 1500, 120, 4)], ""

    monkeypatch.setattr(analytics, "collect", fake_collect)
    w = _window(data_dir, ffmpeg_paths)
    try:
        w.set_page(PUBLISH)
        assert not calls and "No channel" in w.analytics_panel.channel_label.text()
        channel = Channel("UC1", "Skof Clips")
        monkeypatch.setattr(type(w.youtube), "active", property(lambda self: channel))
        monkeypatch.setattr(w.youtube, "has_token", lambda cid: True)
        w.set_page(0)
        w.set_page(PUBLISH)
        assert _wait(lambda: w.analytics_panel.table.rowCount() == 1)
        w.set_page(0)
        w.set_page(PUBLISH)
        assert len(calls) == 1  # loaded once; Refresh loads again
        w.analytics_panel.refresh_button.click()
        assert _wait(lambda: len(calls) == 2 and w._analytics_task is None)
    finally:
        w._set_dirty(False)
        w.close()
