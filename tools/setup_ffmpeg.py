"""Download a Windows x64 FFmpeg build into ``tools/ffmpeg/bin``.

Usage::

    python tools/setup_ffmpeg.py            # download if missing
    python tools/setup_ffmpeg.py --force    # replace an existing copy

The GPL build from BtbN/FFmpeg-Builds includes libx264, libx265 and the
NVENC / Quick Sync / AMF hardware encoders.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

BASE = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
CANDIDATES = (
    "ffmpeg-n7.1-latest-win64-gpl-7.1.zip",
    "ffmpeg-master-latest-win64-gpl.zip",
)
TARGET = Path(__file__).resolve().parent / "ffmpeg" / "bin"
WANTED = ("ffmpeg.exe", "ffprobe.exe")


def _download(url: str, dest: Path) -> None:
    print(f"Downloading {url}")
    with urllib.request.urlopen(url, timeout=60) as response, dest.open("wb") as out:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        while chunk := response.read(1 << 20):
            out.write(chunk)
            done += len(chunk)
            if total:
                print(f"\r  {done * 100 // total:3d}%  {done >> 20} / {total >> 20} MB", end="", flush=True)
    print()


def _extract(archive: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        found = {}
        for member in zf.namelist():
            name = Path(member).name.lower()
            if name in WANTED and "/bin/" in member.replace("\\", "/"):
                found[name] = member
        missing = [w for w in WANTED if w not in found]
        if missing:
            raise RuntimeError(f"Archive does not contain {', '.join(missing)}")
        for name, member in found.items():
            with zf.open(member) as src, (target / name).open("wb") as dst:
                shutil.copyfileobj(src, dst)
            print(f"  installed {target / name}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--force", action="store_true", help="replace an existing copy")
    args = parser.parse_args()

    if sys.platform != "win32":
        print("This script installs the Windows build. On other systems install FFmpeg with your package manager.")
        return 1
    if all((TARGET / w).is_file() for w in WANTED) and not args.force:
        print(f"FFmpeg already installed in {TARGET}")
        return 0

    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "ffmpeg.zip"
        for name in CANDIDATES:
            try:
                _download(BASE + name, archive)
                _extract(archive, TARGET)
                print("Done.")
                return 0
            except Exception as exc:  # noqa: BLE001 - try the next build
                print(f"  failed: {exc}")
    print("Could not download FFmpeg. Install it manually and add it to PATH, "
          "or put ffmpeg.exe and ffprobe.exe into tools/ffmpeg/bin.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
