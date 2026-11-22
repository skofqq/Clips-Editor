"""Find, download and start a newer version of the program.

Each GitHub release of the repository (tag ``v0.9.31``) carries a small
update package, ``TwitchShortsStudio-Update-<version>.exe``: the Inno Setup
installer built with ``/DUpdateOnly``, which holds the program and no models.
It installs over the installed copy (same AppId), so settings, history and
downloaded models in ``%LOCALAPPDATA%\\TwitchShortsStudio`` stay as they are.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app.config import APP_DIR_NAME, APP_VERSION

log = logging.getLogger(__name__)

REPO = "skofqq/Clips-Editor"
API_URL = f"https://api.github.com/repos/{REPO}/releases?per_page=20"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
ASSET_RE = re.compile(r"^TwitchShortsStudio-Update-(?P<version>[0-9][0-9.]*)\.exe$", re.IGNORECASE)
APP_EXE = "TwitchShortsStudio.exe"
RELAUNCH_FLAG = "/RELAUNCH"  # installer.iss starts the program again when it sees this

Progress = Callable[[float, str], None]


class UpdateError(RuntimeError):
    pass


class UpdateCancelled(Exception):
    pass


def parse_version(text: str) -> tuple[int, ...]:
    """``"v0.9.30"`` → ``(0, 9, 30)``; anything after the numbers (``-beta``) is ignored."""
    match = re.match(r"\s*[vV]?(\d+(?:\.\d+)*)", text or "")
    if not match:
        return ()
    parts = [int(p) for p in match.group(1).split(".")]
    while len(parts) > 1 and parts[-1] == 0:  # 1.0 == 1.0.0
        parts.pop()
    return tuple(parts)


def is_newer(candidate: str, current: str = APP_VERSION) -> bool:
    new = parse_version(candidate)
    return bool(new) and new > parse_version(current)


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    tag: str
    title: str
    notes: str  # release description (Markdown)
    page_url: str
    published: str = ""
    asset_name: str = ""
    asset_url: str = ""  # empty = the release has no update package
    size: int = 0
    sha256: str = ""

    @property
    def installable(self) -> bool:
        return bool(self.asset_url)

    @property
    def size_mb(self) -> float:
        return self.size / 1_048_576


def pick_update(releases: list[dict], current: str = APP_VERSION, prerelease: bool = False) -> UpdateInfo | None:
    """The newest release above ``current`` (drafts are skipped, pre-releases
    only when asked). ``None`` when the program is up to date."""
    best: tuple[tuple[int, ...], dict] | None = None
    for release in releases:
        if not isinstance(release, dict) or release.get("draft"):
            continue
        if release.get("prerelease") and not prerelease:
            continue
        version = parse_version(str(release.get("tag_name") or ""))
        if not version or version <= parse_version(current):
            continue
        if best is None or version > best[0]:
            best = (version, release)
    if best is None:
        return None
    version, release = best
    tag = str(release.get("tag_name") or "")
    info = dict(
        version=tag.strip().lstrip("vV"),
        tag=tag,
        title=str(release.get("name") or tag),
        notes=str(release.get("body") or ""),
        page_url=str(release.get("html_url") or RELEASES_PAGE),
        published=str(release.get("published_at") or ""),
    )
    assets = [a for a in release.get("assets") or [] if isinstance(a, dict)]
    asset = next((a for a in assets if ASSET_RE.match(str(a.get("name") or ""))), None)
    if asset is not None:
        digest = str(asset.get("digest") or "")
        info.update(asset_name=str(asset["name"]), asset_url=str(asset.get("browser_download_url") or ""),
                    size=int(asset.get("size") or 0),
                    sha256=digest[7:].lower() if digest.lower().startswith("sha256:") else "")
    return UpdateInfo(**info)


def _request(url: str, accept: str = "application/vnd.github+json") -> urllib.request.Request:
    return urllib.request.Request(url, headers={"Accept": accept, "User-Agent": f"{APP_DIR_NAME}/{APP_VERSION}",
                                                "X-GitHub-Api-Version": "2022-11-28"})


def fetch_releases(url: str = API_URL, timeout: float = 20) -> list[dict]:
    try:
        with urllib.request.urlopen(_request(url), timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            raise UpdateError("GitHub asked to wait a bit (too many checks). Try again later.") from exc
        raise UpdateError(f"GitHub answered {exc.code} {exc.reason}.") from exc
    except (OSError, ValueError) as exc:
        raise UpdateError(f"Could not reach GitHub: {getattr(exc, 'reason', exc)}") from exc
    if not isinstance(data, list):
        raise UpdateError("GitHub sent an unexpected answer.")
    return data


def check_for_update(current: str = APP_VERSION, prerelease: bool = False, url: str = "") -> UpdateInfo | None:
    info = pick_update(fetch_releases(url or releases_url()), current, prerelease)
    log.info("Update check: %s", f"{info.version} available" if info else f"{current} is the newest")
    return info


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _complete(path: Path, info: UpdateInfo) -> bool:
    if not path.is_file() or path.stat().st_size == 0:
        return False
    if info.size and path.stat().st_size != info.size:
        return False
    return not info.sha256 or _sha256(path) == info.sha256


def download_update(info: UpdateInfo, folder: Path, progress: Progress | None = None,
                    cancel: threading.Event | None = None) -> Path:
    """Download the update package into ``folder`` (kept if already there),
    checking its size and SHA-256 against what GitHub lists."""
    if not info.installable:
        raise UpdateError("This release has no update package. Download it from the release page.")
    progress = progress or (lambda f, m: None)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / info.asset_name
    if _complete(target, info):
        progress(1.0, "Update downloaded")
        return target
    part = target.with_name(target.name + ".part")
    try:
        with urllib.request.urlopen(_request(info.asset_url, "application/octet-stream"), timeout=60) as response, \
                open(part, "wb") as out:
            total = int(response.headers.get("Content-Length") or info.size or 0)
            done = 0
            while True:
                if cancel is not None and cancel.is_set():
                    raise UpdateCancelled()
                block = response.read(1 << 20)
                if not block:
                    break
                out.write(block)
                done += len(block)
                if total:
                    progress(min(0.99, done / total), f"Downloading {done / 1_048_576:.0f} / {total / 1_048_576:.0f} MB")
                else:
                    progress(0.0, f"Downloading {done / 1_048_576:.0f} MB")
    except UpdateCancelled:
        part.unlink(missing_ok=True)
        raise
    except OSError as exc:
        part.unlink(missing_ok=True)
        raise UpdateError(f"Could not download the update: {getattr(exc, 'reason', exc)}") from exc
    if info.size and part.stat().st_size != info.size:
        size = part.stat().st_size
        part.unlink(missing_ok=True)
        raise UpdateError(f"The download stopped early ({size} of {info.size} bytes). Try again.")
    if info.sha256 and _sha256(part) != info.sha256:
        part.unlink(missing_ok=True)
        raise UpdateError("The downloaded update is damaged (checksum does not match). Try again.")
    part.replace(target)
    progress(1.0, "Update downloaded")
    return target


def can_install_in_place() -> bool:
    """True for the installed program (Inno Setup put an uninstaller next to
    the exe). From the source folder there is nothing to install over."""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return False
    folder = Path(sys.executable).resolve().parent
    return any(folder.glob("unins*.exe"))


def installer_arguments(log_file: Path | None = None) -> list[str]:
    """Inno Setup switches: a small progress window and nothing to click; it
    closes the program if it still runs and starts it again afterwards."""
    args = ["/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS", "/SP-", RELAUNCH_FLAG]
    if log_file is not None:
        args.append(f"/LOG={log_file}")
    return args


def start_installer(installer: Path, log_file: Path | None = None) -> bool:
    """Start the update package on its own; the program then quits."""
    from PySide6.QtCore import QProcess

    ok, _pid = QProcess.startDetached(str(installer), installer_arguments(log_file), str(Path(installer).parent))
    log.info("Update installer %s started: %s", installer, ok)
    return bool(ok)


def clean_downloads(folder: Path, current: str = APP_VERSION) -> int:
    """Delete update packages for this version or older (already installed)."""
    removed = 0
    folder = Path(folder)
    if not folder.is_dir():
        return 0
    for path in folder.iterdir():
        name = path.name[:-5] if path.name.endswith(".part") else path.name
        match = ASSET_RE.match(name)
        if match and not is_newer(match.group("version"), current):
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def downloads_dir(cache_dir: Path) -> Path:
    return Path(cache_dir) / "updates"


def releases_url() -> str:
    """``TSS_UPDATE_URL`` points the check at another releases list (testing)."""
    return os.environ.get("TSS_UPDATE_URL", "") or API_URL
