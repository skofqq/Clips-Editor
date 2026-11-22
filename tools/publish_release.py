"""Publish a GitHub release with the update package, so installed copies of
the program find it under Help -> Check for Updates.

    .venv\\Scripts\\python tools\\publish_release.py            # version from app/config.py
    .venv\\Scripts\\python tools\\publish_release.py --full     # also the full setup (.exe + .bin)

Needs a GitHub token with write access to the repository's contents
(fine-grained token: Repository access = Clips-Editor, Contents = Read and write).
It is read from GITHUB_TOKEN, or asked once and kept in Windows Credential Manager.
Release notes come from docs/releases/<version>.md (shown in the program's update window).
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import APP_VERSION  # noqa: E402
from core.update.github import REPO  # noqa: E402

API = f"https://api.github.com/repos/{REPO}"
KEYRING_SERVICE = "TwitchShortsStudio-release"


def token() -> str:
    value = os.environ.get("GITHUB_TOKEN", "").strip()
    if value:
        return value
    try:
        import keyring

        value = keyring.get_password(KEYRING_SERVICE, "github") or ""
    except Exception:  # noqa: BLE001 - no keychain: just ask
        keyring = None
    if not value:
        value = getpass.getpass("GitHub token (Contents: read and write): ").strip()
        if value and keyring is not None:
            keyring.set_password(KEYRING_SERVICE, "github", value)
    if not value:
        sys.exit("No token.")
    return value


def call(method: str, url: str, auth: str, body: dict | None = None, data=None, headers: dict | None = None):
    head = {"Authorization": f"Bearer {auth}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "TwitchShortsStudio-release"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        head["Content-Type"] = "application/json"
    head.update(headers or {})
    request = urllib.request.Request(url, data=data, method=method, headers=head)
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and method == "GET":
            return None
        sys.exit(f"GitHub: {exc.code} {exc.reason}\n{exc.read().decode('utf-8', 'replace')}")
    return json.loads(raw) if raw else None


def release_notes(version: str) -> str:
    path = ROOT / "docs" / "releases" / f"{version}.md"
    return path.read_text(encoding="utf-8") if path.is_file() else f"Twitch Shorts Studio {version}"


def upload(release: dict, path: Path, auth: str) -> None:
    for asset in release.get("assets") or []:
        if asset.get("name") == path.name:
            call("DELETE", f"{API}/releases/assets/{asset['id']}", auth)
    url = (release["upload_url"].split("{")[0] + "?" + urllib.parse.urlencode({"name": path.name}))
    size = path.stat().st_size
    print(f"Uploading {path.name} ({size / 1_048_576:.0f} MB)…", flush=True)
    with open(path, "rb") as stream:
        asset = call("POST", url, auth, data=stream,
                     headers={"Content-Type": "application/octet-stream", "Content-Length": str(size)})
    print(f"  done: {asset.get('browser_download_url')}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", default=APP_VERSION)
    parser.add_argument("--full", action="store_true", help="also upload the full setup (.exe and .bin files)")
    parser.add_argument("--prerelease", action="store_true", help="mark as a pre-release (not offered by default)")
    parser.add_argument("--target", default="", help="branch or commit for a new tag (default: the main branch)")
    args = parser.parse_args()
    version = args.version
    folder = ROOT / "build" / "installer"
    update = folder / f"TwitchShortsStudio-Update-{version}.exe"
    if not update.is_file():
        sys.exit(f"{update} is missing. Run build_installer.bat update first.")
    files = [update]
    if args.full:
        files += sorted(folder.glob(f"TwitchShortsStudio-Setup-{version}*"))
    auth = token()
    tag = f"v{version}"
    release = call("GET", f"{API}/releases/tags/{tag}", auth)
    if release is None:
        body = {"tag_name": tag, "name": f"Twitch Shorts Studio {version}", "body": release_notes(version),
                "draft": False, "prerelease": args.prerelease}
        if args.target:
            body["target_commitish"] = args.target
        release = call("POST", f"{API}/releases", auth, body=body)
        print(f"Release {tag} created.")
    else:
        print(f"Release {tag} already exists: replacing its files.")
    for path in files:
        upload(release, path, auth)
    print(f"Done: {release.get('html_url')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
