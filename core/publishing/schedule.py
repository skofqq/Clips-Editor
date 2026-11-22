"""Publishing schedule: ready Shorts spread over the week's time slots.

The user picks the days and times Shorts should go out ("Mon–Fri at 18:00",
or "every day at 12:00 and 20:00"). Each Short put in the queue takes the next
free slot, in queue order, unless it was given a fixed time.

YouTube does the waiting itself: a Short is uploaded early as private with
``publishAt`` and YouTube makes it public at the slot, so the computer may
be off by then. The app only has to be open long enough to upload, and the
free API quota allows about six uploads a day, so the rest wait for the next
day. TikTok and Instagram cannot schedule through their APIs: when asked,
the app posts there itself at the slot time while it is running.

Stored as one JSON file in the user data folder, covers as JPEG files in a
folder next to it.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

VERSION = 1
WAITING, UPLOADING, SCHEDULED, FAILED = "waiting", "uploading", "scheduled", "failed"
TIKTOK, INSTAGRAM = "tiktok", "instagram"
POST_WAIT, POST_SENDING, POST_DONE, POST_FAILED = "wait", "posting", "done", "failed"
LEAD = timedelta(minutes=30)  # a slot closer than this is skipped: the upload needs time
KEEP_DONE = timedelta(days=7)  # Shorts that went out are dropped from the list after this
UPLOAD_UNITS, DAILY_UNITS = 1600, 10_000  # YouTube Data API quota: videos.insert and the free daily total
UPLOADS_PER_DAY = DAILY_UNITS // UPLOAD_UNITS
DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _now() -> datetime:
    return datetime.now().astimezone()


def parse_times(text: str) -> tuple[str, ...]:
    """"18:00, 9.30 21" → ("09:30", "18:00", "21:00"); bad parts are left out."""
    found: set[str] = set()
    for raw in text.replace(";", ",").replace(" ", ",").split(","):
        raw = raw.strip().replace(".", ":")
        if not raw:
            continue
        hour, _, minute = raw.partition(":")
        try:
            h, m = int(hour), int(minute or 0)
        except ValueError:
            continue
        if 0 <= h < 24 and 0 <= m < 60:
            found.add(f"{h:02d}:{m:02d}")
    return tuple(sorted(found))


def _clock(text: str) -> time:
    h, m = text.split(":")
    return time(int(h), int(m))


@dataclass(frozen=True)
class Rule:
    """When Shorts go out: weekdays (0 = Monday) and times of day (local)."""

    days: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6)
    times: tuple[str, ...] = ("18:00",)

    @property
    def per_week(self) -> int:
        return len(self.days) * len(self.times)

    @property
    def text(self) -> str:
        if not self.per_week:
            return "No slots"
        days = "Every day" if len(self.days) == 7 else ", ".join(DAY_NAMES[d] for d in sorted(self.days))
        return f"{days} at {', '.join(self.times)}"

    def to_dict(self) -> dict:
        return {"days": list(self.days), "times": list(self.times)}

    @classmethod
    def from_dict(cls, data: dict | None) -> "Rule":
        data = data or {}
        days = tuple(sorted({int(d) for d in data.get("days", cls.days) if str(d).isdigit() and 0 <= int(d) < 7}))
        times = parse_times(",".join(str(t) for t in data.get("times", cls.times)))
        return cls(days, times)

    def slots(self, start: datetime, limit: int = 200, days_ahead: int = 120):
        """Slot times (local, aware) from ``start`` on, earliest first."""
        if not self.per_week:
            return
        start = start if start.tzinfo else start.astimezone()
        clocks = [_clock(t) for t in self.times]
        day = start.date()
        given = 0
        for _ in range(days_ahead):
            if day.weekday() in self.days:
                for clock in clocks:
                    moment = datetime.combine(day, clock).astimezone()
                    if moment >= start:
                        yield moment
                        given += 1
                        if given >= limit:
                            return
            day += timedelta(days=1)


@dataclass(frozen=True)
class QueueItem:
    """One Short waiting for its slot, with everything the upload needs."""

    id: str
    file: str
    title: str
    description: str = ""
    tags: tuple[str, ...] = ()
    category_id: str = "20"
    language: str = ""
    made_for_kids: bool = False
    captions_srt: str = ""
    captions_language: str = ""
    cover: str = ""  # JPEG file name in the covers folder ("" = none)
    source: str = ""  # the clip it was made from
    variant: str = ""  # kind of title and cover, for History
    when: str = ""  # ISO local time of its slot ("" = not placed yet)
    pinned: bool = False  # the user chose this time; automatic placing leaves it alone
    status: str = WAITING
    channel_id: str = ""  # "" = the active channel at upload time
    channel_title: str = ""
    video_id: str = ""
    url: str = ""
    error: str = ""
    posts: dict = field(default_factory=dict)  # tiktok / instagram → {"state", "url", "error"}
    added_at: str = ""

    @property
    def moment(self) -> datetime | None:
        if not self.when:
            return None
        try:
            value = datetime.fromisoformat(self.when)
        except ValueError:
            return None
        return value if value.tzinfo else value.astimezone()

    @property
    def movable(self) -> bool:
        """Not on YouTube yet, so its time and text can still change."""
        return self.status in (WAITING, FAILED)

    def is_out(self, now: datetime | None = None) -> bool:
        """Uploaded and its slot has passed: the Short is public."""
        m = self.moment
        return self.status == SCHEDULED and m is not None and m <= (now or _now())

    def status_text(self, now: datetime | None = None) -> str:
        if self.status == SCHEDULED:
            return "Out on YouTube" if self.is_out(now) else "Uploaded, waits for its time"
        if self.status == UPLOADING:
            return "Uploading…"
        if self.status == FAILED:
            return f"Failed: {self.error}" if self.error else "Failed"
        return "Waiting to upload" if self.when else "No free slot"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["tags"] = list(self.tags)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "QueueItem":
        known = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        known["tags"] = tuple(str(t) for t in known.get("tags") or ())
        known["pinned"] = bool(known.get("pinned"))
        known["made_for_kids"] = bool(known.get("made_for_kids"))
        posts = known.get("posts")
        known["posts"] = {str(k): dict(v) for k, v in posts.items() if isinstance(v, dict)} \
            if isinstance(posts, dict) else {}
        for key in cls.__dataclass_fields__:
            if key in known and key not in ("tags", "pinned", "made_for_kids", "posts"):
                known[key] = str(known[key] or "")
        if known.get("status") == UPLOADING:  # the app closed mid-upload: try again
            known["status"] = WAITING
        for post in known["posts"].values():
            if post.get("state") == POST_SENDING:  # closed while posting: it may be up, do not post twice
                post.update(state=POST_FAILED, error="The app closed while posting; check the profile.")
        return cls(**known)


def new_item(file: str | Path, title: str, **fields) -> QueueItem:
    return QueueItem(uuid.uuid4().hex[:12], str(file), title,
                     added_at=datetime.now().isoformat(timespec="seconds"), **fields)


def _minute(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc).replace(second=0, microsecond=0)


def arrange(items: list[QueueItem], rule: Rule, now: datetime | None = None) -> list[QueueItem]:
    """Give each movable Short without a fixed time the next free slot, in list order.

    Slots taken by fixed-time or already-uploaded Shorts are skipped. A fixed
    time that has passed (the Short never went up) is released and the Short
    takes the next free slot like the others.
    """
    now = now or _now()
    earliest = now + LEAD
    taken: set[datetime] = set()
    result = list(items)
    for i, item in enumerate(result):
        m = item.moment
        if item.status in (SCHEDULED, UPLOADING) and m is not None:
            taken.add(_minute(m))
        elif item.movable and item.pinned:
            if m is not None and m >= earliest:
                taken.add(_minute(m))
            else:
                result[i] = replace(item, pinned=False)
    free = (s for s in rule.slots(earliest) if _minute(s) not in taken)
    for i, item in enumerate(result):
        if not item.movable or item.pinned:
            continue
        slot = next(free, None)
        result[i] = replace(item, when=slot.isoformat(timespec="minutes") if slot else "")
    return result


def ordered(items: list[QueueItem]) -> list[QueueItem]:
    """By time; Shorts without a slot at the end, in queue order."""
    far = datetime.max.replace(tzinfo=timezone.utc)
    return sorted(items, key=lambda i: (i.moment or far).astimezone(timezone.utc))


def move(items: list[QueueItem], item_id: str, step: int) -> list[QueueItem]:
    """Swap a Short with the next (step 1) or previous (-1) movable one in time order.

    Swapping the list positions is enough for automatic Shorts; fixed times swap too.
    """
    by_time = [i for i in ordered(items) if i.movable]
    index = next((n for n, i in enumerate(by_time) if i.id == item_id), None)
    if index is None or not 0 <= index + step < len(by_time):
        return list(items)
    a, b = by_time[index], by_time[index + step]
    result = list(items)
    ia, ib = result.index(a), result.index(b)
    if a.pinned or b.pinned:  # fixed times stay with the slot: swap the times too
        a2, b2 = replace(a, when=b.when, pinned=b.pinned), replace(b, when=a.when, pinned=a.pinned)
        result[ia], result[ib] = b2, a2
    else:
        result[ia], result[ib] = b, a
    return result


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def week(items: list[QueueItem], monday: date) -> list[list[QueueItem]]:
    """The Shorts of each day Monday..Sunday of that week, by time."""
    days: list[list[QueueItem]] = [[] for _ in range(7)]
    for item in ordered(items):
        m = item.moment
        if m is None:
            continue
        offset = (m.astimezone().date() - monday).days
        if 0 <= offset < 7:
            days[offset].append(item)
    return days


def next_upload(items: list[QueueItem], now: datetime | None = None) -> QueueItem | None:
    """The waiting Short whose slot comes first (with a slot far enough ahead)."""
    earliest = (now or _now()) + LEAD
    for item in ordered(items):
        m = item.moment
        if item.status == WAITING and m is not None and m >= earliest:
            return item
    return None


def due_posts(items: list[QueueItem], services: tuple[str, ...], now: datetime | None = None
              ) -> list[tuple[QueueItem, str]]:
    """(Short, service) pairs whose slot has come and that were not posted there yet."""
    now = now or _now()
    result = []
    for item in ordered(items):
        m = item.moment
        if m is None or m > now or item.status in (UPLOADING,):
            continue
        if item.status != SCHEDULED and now - m < timedelta(hours=1):
            continue  # YouTube has not got it yet: post together once it is up (or an hour late at most)
        for service in services:
            state = (item.posts.get(service) or {}).get("state")
            if state == POST_WAIT and now - m <= timedelta(days=2):
                result.append((item, service))
    return result


def quota_reset(now: datetime | None = None) -> datetime:
    """When YouTube's daily API quota starts over: midnight Pacific Time."""
    now = now or _now()
    try:
        from zoneinfo import ZoneInfo

        pacific = ZoneInfo("America/Los_Angeles")
    except Exception:  # noqa: BLE001 - no time zone data on this system
        pacific = timezone(timedelta(hours=-8))
    local = now.astimezone(pacific)
    midnight = datetime.combine(local.date() + timedelta(days=1), time(0, 5), tzinfo=pacific)
    return midnight.astimezone()


class PublishSchedule:
    """The queue, its rule and options, saved after every change."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.covers = self.path.parent / "schedule_covers"
        self.rule = Rule()
        self.items: list[QueueItem] = []
        self.auto_upload = True
        self.services: tuple[str, ...] = ()  # also post to these at the slot time
        self.paused_until = ""  # ISO time: the YouTube quota is used up until then
        self._load()

    # ------------------------------------------------------------ storage
    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("Could not read the publishing schedule: %s", exc)
            return
        if not isinstance(raw, dict):
            return
        self.rule = Rule.from_dict(raw.get("rule"))
        self.auto_upload = bool(raw.get("auto_upload", True))
        self.services = tuple(s for s in raw.get("services") or () if s in (TIKTOK, INSTAGRAM))
        self.paused_until = str(raw.get("paused_until") or "")
        for data in raw.get("items") or ():
            try:
                self.items.append(QueueItem.from_dict(data))
            except (TypeError, ValueError, AttributeError) as exc:
                log.warning("Skipped a broken schedule entry: %s", exc)

    def save(self) -> None:
        data = {"version": VERSION, "rule": self.rule.to_dict(), "auto_upload": self.auto_upload,
                "services": list(self.services), "paused_until": self.paused_until,
                "items": [i.to_dict() for i in self.items]}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    # ------------------------------------------------------------ changes
    def get(self, item_id: str) -> QueueItem | None:
        return next((i for i in self.items if i.id == item_id), None)

    def arrange(self, now: datetime | None = None) -> None:
        self.items = arrange(self.items, self.rule, now)

    def set_rule(self, rule: Rule, now: datetime | None = None) -> None:
        self.rule = rule
        self.arrange(now)
        self.save()

    def add(self, item: QueueItem, now: datetime | None = None) -> QueueItem:
        """Append (or replace the waiting entry of the same file) and place it."""
        key = str(Path(item.file)).casefold()
        old = next((i for i in self.items if i.movable and str(Path(i.file)).casefold() == key), None)
        if old is not None:
            item = replace(item, id=old.id, when=old.when, pinned=old.pinned, cover=item.cover or old.cover,
                           posts=old.posts)
            self.items[self.items.index(old)] = item
        else:
            if self.services and not item.posts:
                item = replace(item, posts={s: {"state": POST_WAIT} for s in self.services})
            self.items.append(item)
        self.arrange(now)
        self.save()
        return self.get(item.id) or item

    def update(self, item: QueueItem, rearrange: bool = False, now: datetime | None = None) -> None:
        for n, old in enumerate(self.items):
            if old.id == item.id:
                self.items[n] = item
                break
        if rearrange:
            self.arrange(now)
        self.save()

    def remove(self, item_id: str) -> None:
        item = self.get(item_id)
        if item is None:
            return
        self.items.remove(item)
        if item.cover:
            try:
                (self.covers / item.cover).unlink()
            except OSError:
                pass
        self.arrange()
        self.save()

    def move(self, item_id: str, step: int) -> None:
        self.items = move(self.items, item_id, step)
        self.arrange()
        self.save()

    def set_services(self, services: tuple[str, ...]) -> None:
        """Turning a service on asks for posts of Shorts still ahead; off drops the waiting ones."""
        self.services = tuple(s for s in (TIKTOK, INSTAGRAM) if s in services)
        now = _now()
        for n, item in enumerate(self.items):
            posts = dict(item.posts)
            for service in (TIKTOK, INSTAGRAM):
                state = (posts.get(service) or {}).get("state")
                if service in self.services and state is None and (item.moment is None or item.moment > now):
                    posts[service] = {"state": POST_WAIT}
                elif service not in self.services and state == POST_WAIT:
                    posts.pop(service)
            self.items[n] = replace(item, posts=posts)
        self.save()

    def set_post(self, item_id: str, service: str, state: str, url: str = "", error: str = "") -> None:
        item = self.get(item_id)
        if item is not None:
            posts = {**item.posts, service: {"state": state, "url": url, "error": error}}
            self.update(replace(item, posts=posts))

    def set_cover(self, item_id: str, data: bytes) -> None:
        item = self.get(item_id)
        if item is None or not data:
            return
        self.covers.mkdir(parents=True, exist_ok=True)
        name = f"{item.id}.jpg"
        (self.covers / name).write_bytes(data)
        self.update(replace(item, cover=name))

    def cover_path(self, item: QueueItem) -> Path | None:
        if not item.cover:
            return None
        path = self.covers / item.cover
        return path if path.is_file() else None

    def prune(self, now: datetime | None = None) -> None:
        """Drop Shorts that went out more than a week ago (History keeps them)."""
        now = now or _now()
        keep = []
        for item in self.items:
            m = item.moment
            if item.status == SCHEDULED and m is not None and now - m > KEEP_DONE:
                if item.cover:
                    try:
                        (self.covers / item.cover).unlink()
                    except OSError:
                        pass
                continue
            keep.append(item)
        if len(keep) != len(self.items):
            self.items = keep
            self.save()

    # ------------------------------------------------------------ quota
    def paused(self, now: datetime | None = None) -> bool:
        if not self.paused_until:
            return False
        try:
            until = datetime.fromisoformat(self.paused_until)
        except ValueError:
            return False
        return (now or _now()) < (until if until.tzinfo else until.astimezone())

    def pause_for_quota(self, now: datetime | None = None) -> datetime:
        until = quota_reset(now)
        self.paused_until = until.isoformat(timespec="minutes")
        self.save()
        return until

    def waiting(self) -> list[QueueItem]:
        return [i for i in self.items if i.status == WAITING]
