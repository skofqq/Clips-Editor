"""Video metadata for videos.insert, with YouTube's limits checked up front."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

PRIVATE, UNLISTED, PUBLIC = "private", "unlisted", "public"
PRIVACY = (PRIVATE, UNLISTED, PUBLIC)

MAX_TITLE = 100
MAX_DESCRIPTION_BYTES = 5000
MAX_TAGS_CHARS = 500

# Common categories (IDs are the same in every region that has them).
CATEGORIES = {
    "20": "Gaming",
    "24": "Entertainment",
    "22": "People & Blogs",
    "23": "Comedy",
    "17": "Sports",
    "10": "Music",
    "1": "Film & Animation",
    "28": "Science & Technology",
}
DEFAULT_CATEGORY = "20"


def parse_tags(text: str) -> tuple[str, ...]:
    seen: dict[str, str] = {}
    for raw in text.replace("\n", ",").split(","):
        tag = " ".join(raw.split()).lstrip("#").strip()
        if tag and tag.casefold() not in seen:
            seen[tag.casefold()] = tag
    return tuple(seen.values())


def tags_length(tags: tuple[str, ...]) -> int:
    """Length as YouTube counts it: tags with spaces are quoted, commas between."""
    return sum(len(t) + (2 if " " in t else 0) for t in tags) + max(0, len(tags) - 1)


@dataclass(frozen=True)
class VideoMetadata:
    title: str
    description: str = ""
    tags: tuple[str, ...] = ()
    category_id: str = DEFAULT_CATEGORY
    privacy: str = PRIVATE
    publish_at: datetime | None = None  # scheduled publication (UTC aware); needs privacy=private
    language: str = ""  # "ru", "en" or "" (not set)
    made_for_kids: bool = False

    def problems(self, now: datetime | None = None) -> list[str]:
        """Everything the API would reject, as messages for the user."""
        issues = []
        title = self.title.strip()
        if not title:
            issues.append("The title is empty.")
        if len(title) > MAX_TITLE:
            issues.append(f"The title is {len(title)} characters; YouTube allows {MAX_TITLE}.")
        for name, text in (("title", self.title), ("description", self.description)):
            if "<" in text or ">" in text:
                issues.append(f"The {name} must not contain < or >.")
        size = len(self.description.encode("utf-8"))
        if size > MAX_DESCRIPTION_BYTES:
            issues.append(f"The description is {size} bytes; YouTube allows {MAX_DESCRIPTION_BYTES}.")
        if tags_length(self.tags) > MAX_TAGS_CHARS:
            issues.append(f"Tags are {tags_length(self.tags)} characters together; YouTube allows {MAX_TAGS_CHARS}.")
        if any("<" in t or ">" in t for t in self.tags):
            issues.append("Tags must not contain < or >.")
        if self.category_id not in CATEGORIES:
            issues.append("Choose a category.")
        if self.privacy not in PRIVACY:
            issues.append("Choose the visibility.")
        if self.publish_at is not None:
            now = now or datetime.now(timezone.utc)
            if self.publish_at.tzinfo is None:
                issues.append("The scheduled time has no time zone.")
            elif self.publish_at <= now:
                issues.append("The scheduled time is in the past.")
        return issues

    def body(self) -> dict[str, Any]:
        """Request body for ``videos.insert`` (parts snippet and status)."""
        snippet: dict[str, Any] = {
            "title": self.title.strip(),
            "description": self.description,
            "categoryId": self.category_id,
        }
        if self.tags:
            snippet["tags"] = list(self.tags)
        if self.language:
            snippet["defaultLanguage"] = self.language
            snippet["defaultAudioLanguage"] = self.language
        status: dict[str, Any] = {
            "privacyStatus": PRIVATE if self.publish_at is not None else self.privacy,
            "selfDeclaredMadeForKids": self.made_for_kids,
        }
        if self.publish_at is not None:
            status["publishAt"] = self.publish_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        return {"snippet": snippet, "status": status}
