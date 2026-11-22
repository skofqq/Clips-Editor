"""Media list grouped by month → day, folding and the remembered settings."""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta

import pytest

from core.media import dates
from core.media.dates import MediaGroupsStore, date_from_name
from core.media.probe import MediaInfo


@pytest.mark.parametrize("name, expected", [
    ("2025-11-20 sasavot - clip.mp4", date(2025, 11, 20)),
    ("clip_20250304_highlight.mkv", date(2025, 3, 4)),
    ("stream 2024.01.31 part.mp4", date(2024, 1, 31)),
    ("Клип 20.11.2025.mp4", date(2025, 11, 20)),
    ("2025_02_30 bad day.mp4", None),        # not a real date
    ("clip 1920x1080.mp4", None),
    ("id 1234520251120999.mp4", None),        # digits inside a longer number
    ("2099-01-01 future.mp4", None),
    ("plain name.mp4", None),
])
def test_date_from_name(name, expected):
    assert date_from_name(name) == expected


def test_labels():
    today = date(2026, 11, 22)
    assert dates.month_label("2026-11", today) == "November"
    assert dates.month_label("2025-12", today) == "December 2025"
    assert dates.day_label("2026-11-22", today) == "Today"
    assert dates.day_label("2026-11-21", today) == "Yesterday"
    assert dates.day_label("2026-11-20", today) == "Fri, 20 Nov"
    assert dates.day_label("2025-11-20", today) == "Thu, 20 Nov 2025"


def _clip(tmp_path, name, when: datetime) -> MediaInfo:
    path = tmp_path / name
    path.write_bytes(b"x")
    stamp = when.timestamp()
    os.utime(path, (stamp, stamp))
    return MediaInfo(path=path, size_bytes=1, duration_s=12.0, container="mp4", bit_rate=None, video=None)


def test_store_clip_time_and_persistence(tmp_path):
    named = _clip(tmp_path, "2025-10-05 stream.mp4", datetime(2025, 11, 1, 18, 0))
    plain = _clip(tmp_path, "plain.mp4", datetime(2025, 11, 1, 18, 30))
    store = MediaGroupsStore.load(tmp_path / "groups.json")
    assert store.note_import(named.path, datetime(2026, 1, 2, 9, 0))
    assert not store.note_import(named.path)  # the first import is kept
    assert store.clip_time(named.path).date() == date(2025, 10, 5)  # the name wins over the file time
    assert store.clip_time(plain.path) == datetime(2025, 11, 1, 18, 30)
    store.source = dates.SOURCE_IMPORT
    assert store.clip_time(named.path) == datetime(2026, 1, 2, 9, 0)
    store.grouping = dates.GROUP_DAY
    store.collapsed = {"2025-10"}
    store.save()

    again = MediaGroupsStore.load(tmp_path / "groups.json")
    assert (again.grouping, again.source, again.collapsed) == (dates.GROUP_DAY, dates.SOURCE_IMPORT, {"2025-10"})
    assert again.import_time(named.path) == datetime(2026, 1, 2, 9, 0)
    (tmp_path / "bad.json").write_text("{nope", encoding="utf-8")
    assert MediaGroupsStore.load(tmp_path / "bad.json").grouping == dates.GROUP_MONTH_DAY


def _visible(panel):
    rows = []
    for i in range(panel.list.count()):
        item = panel.list.item(i)
        if item.isHidden():
            continue
        info = item.data(panel_mod()._ROLE_MEDIA)
        rows.append(info.path.name if info is not None else f"[{item.text()}]")
    return rows


def panel_mod():
    import ui.panels.media_panel as mod
    return mod


def test_panel_groups_fold_and_remember(qapp, tmp_path):
    from ui.panels.media_panel import MediaPanel

    store_path = tmp_path / "data" / "media_groups.json"
    panel = MediaPanel(store=MediaGroupsStore.load(store_path))
    now = datetime.now().replace(microsecond=0)
    old = datetime(2025, 3, 14, 20, 0)
    clips = [
        _clip(tmp_path, "a.mp4", old),
        _clip(tmp_path, "b.mp4", old + timedelta(hours=1)),
        _clip(tmp_path, "c.mp4", datetime(2025, 3, 2, 12, 0)),
        _clip(tmp_path, "d.mp4", now),
    ]
    selected = []
    panel.media_selected.connect(selected.append)
    for info in clips:
        panel.add_media(info)
    assert panel.media_count() == 4 and panel.count_label.text() == "4 videos"
    month_now = dates.month_label(dates.month_key(now.date()))
    assert _visible(panel) == [f"[{month_now}]", "[Today]", "d.mp4",
                               "[March 2025]", "[Fri, 14 Mar 2025]", "b.mp4", "a.mp4",
                               "[Sun, 2 Mar 2025]", "c.mp4"]
    counts = {h.text(): h.data(panel_mod()._ROLE_COUNT) for h in panel.headers()}
    assert counts["March 2025"] == 3 and counts["Fri, 14 Mar 2025"] == 2
    assert panel.current_media() is clips[-1]

    # Clicking a header folds it, and it does not change the open clip.
    panel.resize(300, 600)
    panel.show()
    qapp.processEvents()
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    header = next(h for h in panel.headers() if h.text() == "March 2025")
    rect = panel.list.visualItemRect(header)
    QTest.mouseClick(panel.list.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(rect.center()))
    assert panel.is_collapsed("2025-03")
    assert _visible(panel) == [f"[{month_now}]", "[Today]", "d.mp4", "[March 2025]"]
    assert selected[-1] is clips[-1] and panel.current_media() is clips[-1]

    # Painting headers and clips works.
    panel.grab()

    # Folding a day inside an open month.
    panel.set_collapsed("2025-03", False)
    panel.set_collapsed("2025-03-14", True)
    assert "b.mp4" not in _visible(panel) and "c.mp4" in _visible(panel)

    # Selecting a clip in a folded group unfolds it.
    assert panel.select_media(clips[0].path)
    assert not panel.is_collapsed("2025-03-14") and "a.mp4" in _visible(panel)
    assert panel.current_media() is clips[0]

    # Collapse all folds months only; expand all opens everything.
    panel.set_collapsed("2025-03-02", True)
    panel.set_all_collapsed(True)
    assert _visible(panel) == [f"[{month_now}]", "[March 2025]"]
    panel.set_all_collapsed(False)
    assert len(_visible(panel)) == 9

    # Other groupings.
    panel.set_grouping(dates.GROUP_MONTH)
    assert _visible(panel) == [f"[{month_now}]", "d.mp4", "[March 2025]", "b.mp4", "a.mp4", "c.mp4"]
    panel.set_grouping(dates.GROUP_NONE)
    assert _visible(panel) == ["a.mp4", "b.mp4", "c.mp4", "d.mp4"]  # import order
    assert panel.current_media() is clips[0]
    panel.set_grouping(dates.GROUP_DAY)
    panel.set_date_source(dates.SOURCE_IMPORT)  # all imported just now
    assert _visible(panel)[0] == "[Today]" and len(_visible(panel)) == 5

    # Remembered for the next start.
    panel.set_collapsed(dates.day_key(date.today()), True)
    again = MediaGroupsStore.load(store_path)
    assert again.grouping == dates.GROUP_DAY and again.source == dates.SOURCE_IMPORT
    assert dates.day_key(date.today()) in again.collapsed
    assert len(again.imported) == 4

    other = MediaPanel(store=again)
    for info in clips:
        other.add_media(info, select=False)
    assert _visible(other) == ["[Today]"]

    # Removing clips updates the counts and the list.
    panel.remove_media(clips[0].path)
    assert panel.media_count() == 3 and panel.headers()[0].data(panel_mod()._ROLE_COUNT) == 3
    panel.clear()
    assert panel.media_count() == 0 and panel.stack.currentWidget() is panel.empty
    panel.close()


def test_panel_menu_and_keyboard(qapp, tmp_path):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from ui.panels.media_panel import MediaPanel

    panel = MediaPanel()
    for name, when in (("x.mp4", datetime(2025, 5, 1, 10)), ("y.mp4", datetime(2025, 4, 1, 10))):
        panel.add_media(_clip(tmp_path, name, when))
    panel._sync_group_menu()
    assert panel._grouping_actions[dates.GROUP_MONTH_DAY].isChecked()
    panel._grouping_actions[dates.GROUP_DAY].trigger()
    assert panel.store.grouping == dates.GROUP_DAY
    panel._source_actions[dates.SOURCE_IMPORT].trigger()
    assert panel.store.source == dates.SOURCE_IMPORT

    panel.set_grouping(dates.GROUP_MONTH_DAY)
    panel.set_date_source(dates.SOURCE_CLIP)
    header = panel.headers()[0]
    panel.list.setCurrentItem(header)  # keyboard focus on a header keeps the open clip
    assert panel.current_media() is not None
    QTest.keyClick(panel.list, Qt.Key.Key_Left)
    assert panel.is_collapsed(header.data(panel_mod()._ROLE_KEY))
    QTest.keyClick(panel.list, Qt.Key.Key_Space)
    assert not panel.is_collapsed(header.data(panel_mod()._ROLE_KEY))
