"""Download a model file once, with progress, cancel and a .part file."""

from __future__ import annotations

import threading
import urllib.request
from pathlib import Path
from typing import Callable

Progress = Callable[[float, str], None]


class DownloadError(RuntimeError):
    pass


class DownloadCancelled(Exception):
    pass


def fetch(url: str, target: Path, approx_mb: int, label: str, progress: Progress | None = None,
          cancel: threading.Event | None = None) -> Path:
    """Download ``url`` to ``target`` unless it is already there."""
    target = Path(target)
    if target.is_file() and target.stat().st_size > 0:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(target.suffix + ".part")
    progress = progress or (lambda f, m: None)
    try:
        with urllib.request.urlopen(url, timeout=60) as response, open(part, "wb") as out:
            total = int(response.headers.get("Content-Length") or approx_mb * 1_048_576) or 1
            done = 0
            while True:
                if cancel is not None and cancel.is_set():
                    raise DownloadCancelled()
                block = response.read(1 << 20)
                if not block:
                    break
                out.write(block)
                done += len(block)
                progress(min(0.99, done / total),
                         f"Downloading {label}… {done / 1_048_576:.0f} / {total / 1_048_576:.0f} MB")
    except DownloadCancelled:
        part.unlink(missing_ok=True)
        raise
    except OSError as exc:
        part.unlink(missing_ok=True)
        raise DownloadError(f"Could not download {label}: {exc}") from exc
    part.replace(target)
    return target
