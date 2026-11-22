"""Publishing schedule: slots, automatic placing, storage and the Schedule tab."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from core.publishing import schedule as sc
from core.youtube.api import Channel, UploadResult
from core.youtube.errors import YouTubeQuotaError


def local(y, mo, d, h=0, mi=0) -> datetime:
    return datetime(y, mo, d, h, mi).astimezone()


MON = local(2026, 11, 23, 9)  # Monday morning


def test_parse_times():
    assert sc.parse_times("18:00, 9.30 21;7") == ("07:00", "09:30", "18:00", "21:00")
    assert sc.parse_times("25:00, abc, 12:61, 12") == ("12:00",)
    assert sc.parse_times("") == ()


def test_rule_slots_follow_days_and_times():
    rule = sc.Rule(days=(0, 2, 4), times=("12:00", "18:00"))
    slots = list(rule.slots(MON, limit=5))
    assert [(s.weekday(), s.strftime("%H:%M")) for s in slots] == [
        (0, "12:00"), (0, "18:00"), (2, "12:00"), (2, "18:00"), (4, "12:00")]
    assert rule.per_week == 6
    assert rule.text == "Mon, Wed, Fri at 12:00, 18:00"
    assert list(sc.Rule(days=(), times=("12:00",)).slots(MON)) == []
    assert sc.Rule.from_dict(rule.to_dict()) == rule
    assert sc.Rule.from_dict({"days": [9, "1"], "times": ["7:5", "x"]}) == sc.Rule((1,), ("07:05",))


def test_arrange_fills_free_slots_in_order():
    rule = sc.Rule(times=("18:00",))
    items = [sc.new_item(f"/v/{n}.mp4", f"Short {n}") for n in range(3)]
    placed = sc.arrange(items, rule, MON)
    assert [i.moment.day for i in placed] == [23, 24, 25]
    assert all(i.moment.hour == 18 for i in placed)


def test_arrange_skips_taken_and_releases_missed_fixed_time():
    rule = sc.Rule(times=("18:00",))
    up = replace(sc.new_item("/v/a.mp4", "A"), status=sc.SCHEDULED,
                 when=local(2026, 11, 23, 18).isoformat())  # already on YouTube for Monday
    fixed = replace(sc.new_item("/v/b.mp4", "B"), pinned=True, when=local(2026, 11, 24, 18).isoformat())
    missed = replace(sc.new_item("/v/c.mp4", "C"), pinned=True, when=local(2026, 11, 22, 18).isoformat())
    auto = sc.new_item("/v/d.mp4", "D")
    placed = {i.title: i for i in sc.arrange([up, fixed, missed, auto], rule, MON)}
    assert placed["A"].moment.day == 23
    assert placed["B"].moment.day == 24 and placed["B"].pinned
    assert not placed["C"].pinned and placed["C"].moment.day == 25  # first free one after Tuesday
    assert placed["D"].moment.day == 26


def test_arrange_skips_a_slot_too_close_to_now():
    rule = sc.Rule(times=("09:20", "18:00"))
    item = sc.arrange([sc.new_item("/v/a.mp4", "A")], rule, MON)[0]  # 09:20 is 20 min away
    assert item.moment.strftime("%d %H:%M") == "23 18:00"


def test_no_slot_when_rule_is_empty():
    item = sc.arrange([sc.new_item("/v/a.mp4", "A")], sc.Rule(days=()), MON)[0]
    assert item.when == "" and item.status_text() == "No free slot"


def test_move_swaps_order_and_fixed_times():
    rule = sc.Rule(times=("18:00",))
    items = sc.arrange([sc.new_item(f"/v/{n}.mp4", str(n)) for n in range(3)], rule, MON)
    moved = sc.arrange(sc.move(items, items[2].id, -1), rule, MON)
    assert [i.title for i in sc.ordered(moved)] == ["0", "2", "1"]
    pinned = [replace(moved[0], pinned=True)] + moved[1:]
    swapped = sc.move(pinned, pinned[0].id, 1)
    first = next(i for i in swapped if i.title == "0")
    assert first.moment.day == 24 and not first.pinned
    assert sc.move(items, "nope", 1) == items


def test_week_groups_by_day():
    rule = sc.Rule(times=("12:00", "18:00"))
    items = sc.arrange([sc.new_item(f"/v/{n}.mp4", str(n)) for n in range(5)], rule, MON)
    days = sc.week(items, date(2026, 11, 23))
    assert [len(d) for d in days] == [2, 2, 1, 0, 0, 0, 0]
    assert sc.week_start(date(2026, 11, 26)) == date(2026, 11, 23)


def test_next_upload_and_due_posts():
    rule = sc.Rule(times=("18:00",))
    items = sc.arrange([sc.new_item("/v/a.mp4", "A"), sc.new_item("/v/b.mp4", "B")], rule, MON)
    assert sc.next_upload(items, MON).title == "A"
    items[0] = replace(items[0], status=sc.SCHEDULED, posts={sc.TIKTOK: {"state": sc.POST_WAIT}})
    assert sc.next_upload(items, MON).title == "B"
    assert sc.due_posts(items, (sc.TIKTOK,), MON) == []
    evening = local(2026, 11, 23, 18, 1)
    assert [(i.title, s) for i, s in sc.due_posts(items, (sc.TIKTOK,), evening)] == [("A", sc.TIKTOK)]
    assert sc.due_posts(items, (sc.INSTAGRAM,), evening) == []  # not asked for this Short
    assert sc.due_posts(items, (sc.TIKTOK,), evening + timedelta(days=3)) == []  # too late, skipped


def test_quota_reset_is_after_pacific_midnight():
    now = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)  # 04:00 in California
    reset = sc.quota_reset(now)
    assert reset > now
    assert timedelta(hours=19) < reset - now < timedelta(hours=22)


def test_store_round_trip_and_same_file_replaces(tmp_path):
    store = sc.PublishSchedule(tmp_path / "schedule.json")
    store.set_rule(sc.Rule(days=(0, 1, 2, 3, 4), times=("19:00",)))
    store.set_services((sc.TIKTOK,))
    a = store.add(sc.new_item(tmp_path / "a.mp4", "A", tags=("x", "y")))
    assert a.when and a.posts == {sc.TIKTOK: {"state": sc.POST_WAIT}}
    again = store.add(sc.new_item(tmp_path / "A.MP4", "A, better title"))
    assert again.id == a.id and again.when == a.when and len(store.items) == 1
    store.set_cover(a.id, b"jpeg")
    store.update(replace(store.get(a.id), status=sc.UPLOADING))
    reloaded = sc.PublishSchedule(tmp_path / "schedule.json")
    item = reloaded.items[0]
    assert item.title == "A, better title" and item.cover == f"{a.id}.jpg"
    assert item.status == sc.WAITING  # an upload cut off by closing is tried again
    assert reloaded.rule.times == ("19:00",) and reloaded.services == (sc.TIKTOK,)
    assert reloaded.cover_path(item).read_bytes() == b"jpeg"
    reloaded.remove(item.id)
    assert not reloaded.items and not (tmp_path / "schedule_covers" / f"{a.id}.jpg").exists()


def test_store_ignores_broken_file(tmp_path):
    path = tmp_path / "schedule.json"
    path.write_text("{broken", encoding="utf-8")
    assert sc.PublishSchedule(path).items == []
    path.write_text(json.dumps({"items": [{"id": "x"}, {"id": "y", "file": "f", "title": "T",
                                                         "posts": {"tiktok": {"state": "posting"}}}]}))
    store = sc.PublishSchedule(path)
    assert [i.id for i in store.items] == ["y"]
    assert store.items[0].posts["tiktok"]["state"] == sc.POST_FAILED  # never posted twice


def test_services_toggle_and_prune(tmp_path):
    store = sc.PublishSchedule(tmp_path / "s.json")
    store.add(sc.new_item(tmp_path / "a.mp4", "A"))
    store.set_services((sc.INSTAGRAM,))
    assert store.items[0].posts == {sc.INSTAGRAM: {"state": sc.POST_WAIT}}
    store.set_services(())
    assert store.items[0].posts == {}
    old = replace(store.items[0], status=sc.SCHEDULED, when=(datetime.now().astimezone() - timedelta(days=9))
                  .isoformat())
    store.update(old)
    store.prune()
    assert store.items == []


def test_quota_pause(tmp_path):
    store = sc.PublishSchedule(tmp_path / "s.json")
    assert not store.paused()
    until = store.pause_for_quota()
    assert store.paused() and not store.paused(until + timedelta(minutes=1))


# ------------------------------------------------------------------ UI
class FakeYouTube:
    def __init__(self, fail=None) -> None:
        self.channel = Channel("UC1", "My channel")
        self.fail = list(fail or [])
        self.sent = []

    @property
    def accounts(self):
        return [self.channel]

    @property
    def active(self):
        return self.channel

    def has_token(self, channel_id):
        return True

    def client_for(self, channel_id):
        return self


class FakeSocial:
    tiktok_name = ""
    instagram_name = ""


@pytest.fixture
def fake_upload(monkeypatch):
    """Replace the network upload: returns what YouTube would."""
    calls = []

    def upload(client, path, metadata, progress=None, cancel=None, captions_srt="", captions_language="",
               thumbnail=None):
        if client.fail:
            raise client.fail.pop(0)
        calls.append((str(path), metadata, captions_srt, thumbnail() if thumbnail else None))
        return UploadResult(f"vid{len(calls)}", "private",
                            metadata.publish_at.strftime("%Y-%m-%dT%H:%M:%SZ"), "uploaded")

    import ui.youtube.upload_dialog as ud

    monkeypatch.setattr(ud, "_upload", upload)
    return calls


def _wait(qapp, condition, timeout=5.0):
    import time

    end = time.monotonic() + timeout
    while not condition() and time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.01)
    assert condition()


def _panel(tmp_path, youtube):
    from types import SimpleNamespace

    from ui.social.schedule_panel import SchedulePanel

    store = sc.PublishSchedule(tmp_path / "schedule.json")
    store.auto_upload = False
    config = SimpleNamespace(export_dir="", youtube_defaults={}, social_defaults={})
    return SchedulePanel(store, youtube, FakeSocial(), config)


def test_panel_uploads_queue_with_publish_times(qapp, tmp_path, fake_upload):
    youtube = FakeYouTube()
    panel = _panel(tmp_path, youtube)
    files = []
    for n in range(2):
        f = tmp_path / f"short{n}.mp4"
        f.write_bytes(b"video")
        files.append(f)
        panel.add(sc.new_item(f, f"Short {n}", captions_srt="1\n00:00:00,000 --> 00:00:01,000\nhi\n"))
    panel.store.set_cover(panel.store.items[0].id, b"cover")
    assert panel.list.topLevelItemCount() == 2
    results = []
    panel.uploaded.connect(lambda item, result: results.append((item.title, result.video_id)))
    panel.upload_all()
    _wait(qapp, lambda: len(results) == 2)
    assert results == [("Short 0", "vid1"), ("Short 1", "vid2")]
    first, second = fake_upload
    assert first[1].publish_at is not None and first[1].privacy == "private"
    assert first[1].body()["status"]["publishAt"]
    assert first[2].startswith("1\n") and first[3] == b"cover" and second[3] is None
    assert second[1].publish_at > first[1].publish_at
    assert all(i.status == sc.SCHEDULED and i.channel_title == "My channel" for i in panel.store.items)
    assert "goes public" in panel.status_label.text()
    _wait(qapp, lambda: not panel.busy)


def test_panel_quota_error_pauses_until_tomorrow(qapp, tmp_path, fake_upload):
    youtube = FakeYouTube(fail=[YouTubeQuotaError("quota", 403, "quotaExceeded")])
    panel = _panel(tmp_path, youtube)
    f = tmp_path / "a.mp4"
    f.write_bytes(b"v")
    panel.add(sc.new_item(f, "A"))
    panel.upload_all()
    _wait(qapp, lambda: not panel.busy)
    assert panel.store.paused() and panel.store.items[0].status == sc.WAITING
    assert "daily limit" in panel.status_label.text()
    panel.tick()  # paused: the background upload does not start
    assert not panel.busy


def test_panel_missing_file_fails_and_rule_change_moves_items(qapp, tmp_path, fake_upload):
    panel = _panel(tmp_path, FakeYouTube())
    panel.add(sc.new_item(tmp_path / "gone.mp4", "Gone"))
    panel.upload_all()
    assert panel.store.items[0].status == sc.FAILED and "missing" in panel.store.items[0].error
    ok = tmp_path / "b.mp4"
    ok.write_bytes(b"v")
    panel.add(sc.new_item(ok, "B"))
    for n, button in enumerate(panel.day_buttons):
        button.setChecked(n == 5)  # Saturdays only
    panel.times_edit.setText("10:00, 20:00")
    panel._rule_changed()
    assert panel.store.rule == sc.Rule((5,), ("10:00", "20:00"))
    b = next(i for i in panel.store.items if i.title == "B")
    assert b.moment.weekday() == 5
    assert "2 Shorts a week" in panel.rule_label.text()


def test_best_time_button_sets_the_slot(qapp, tmp_path):
    from core.youtube.best_time import BestTime

    panel = _panel(tmp_path, FakeYouTube())
    panel.times_edit.setText("12:00, 19:00")
    panel._rule_changed()
    panel.set_best_time(BestTime(18, 900, 4, 12, 300))
    assert panel.best_button.isEnabled()
    panel.use_best_time()
    assert panel.store.rule.times == ("12:00", "18:00")


def test_week_view_draws_items_and_free_slots(qapp, tmp_path):
    panel = _panel(tmp_path, FakeYouTube())
    panel.store.set_rule(sc.Rule(times=("18:00",)))
    item = panel.add(sc.new_item(tmp_path / "a.mp4", "A"))
    panel.resize(900, 700)
    panel.show()
    qapp.processEvents()
    view = panel.week_view
    assert any(item.id in d_ids for d_ids in ([i.id for i in d] for d in view.days))
    view.repaint()
    rect, item_id = view._chips[0]
    assert item_id == item.id
    assert view.chip_at(rect.center().x(), rect.center().y()) == item.id
    panel.show_week(view.monday + timedelta(days=7))
    assert sum(len(s) for s in view.slots) == 7  # a free slot every day of the next seven
    panel.close()


def test_upload_form_adds_to_schedule(qapp, data_dir, tmp_path):
    from core.youtube.metadata import VideoMetadata

    from app.config import AppConfig
    from ui.main_window.main_window import MainWindow

    w = MainWindow(config=AppConfig.load(data_dir), ffmpeg=None)
    f = tmp_path / "clip_short.mp4"
    f.write_bytes(b"v")
    item = w.schedule.add_from_form({
        "file": f, "metadata": VideoMetadata("My Short", "desc #twitch", ("a",), "20", language="ru"),
        "captions_srt": "", "captions_language": "", "thumbnail": lambda: b"jpeg", "channel_id": ""})
    assert item.title == "My Short" and item.when
    assert w.service_tabs.current() == w.schedule.tab
    _wait(qapp, lambda: w.schedule.store.get(item.id).cover != "")
    assert w.schedule.has_file(f)
    w.schedule.stop()
    w.close()
