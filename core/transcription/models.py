"""Local speech-model management: what is downloaded, how big, download, delete.

faster-whisper keeps models in a Hugging Face cache layout under
``models_dir``: ``models--<org>--<name>/snapshots/<revision>/model.bin``.
This module reads that layout directly, so listing models works offline
and without importing the heavy recognition libraries.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

# Model size → Hugging Face repository (the same names faster-whisper uses).
REPOS = {
    "tiny": "Systran/faster-whisper-tiny",
    "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small",
    "medium": "Systran/faster-whisper-medium",
    "large-v3": "Systran/faster-whisper-large-v3",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    "distil-large-v3": "Systran/faster-distil-whisper-large-v3",
    "large-v3-russian": "bzikst/faster-whisper-large-v3-russian",
    "large-v3-russian-int8": "bzikst/faster-whisper-large-v3-russian-int8",
}

# Approximate download size and a one-line description shown to the user.
INFO = {
    "tiny": (75, "Fastest, rough; for testing"),
    "base": (145, "Fast, simple speech"),
    "small": (485, "Good balance; works well on a CPU"),
    "medium": (1530, "More accurate, slow on a CPU"),
    "large-v3": (3090, "Most accurate; needs a GPU"),
    "large-v3-turbo": (1620, "Nearly large-v3 quality, much faster; best with a GPU"),
    "distil-large-v3": (1510, "English only: large-v3 quality, several times faster"),
    "large-v3-russian": (3090, "large-v3 fine-tuned on Russian: fewer errors in Russian; needs a GPU"),
    "large-v3-russian-int8": (1550, "The Russian model, compressed: half the size, almost the same quality"),
}


@dataclass(frozen=True)
class LocalModel:
    name: str
    repo: str
    path: Path | None  # snapshot folder with model.bin, None if not downloaded
    size_bytes: int
    approx_mb: int
    description: str

    @property
    def downloaded(self) -> bool:
        return self.path is not None


def _cache_dir(models_dir: Path, repo: str) -> Path:
    return Path(models_dir) / ("models--" + repo.replace("/", "--"))


def _snapshot(models_dir: Path, repo: str) -> Path | None:
    snapshots = _cache_dir(models_dir, repo) / "snapshots"
    if not snapshots.is_dir():
        return None
    for folder in sorted(snapshots.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if (folder / "model.bin").exists():
            return folder
    return None


def folder_size(path: Path) -> int:
    total = 0
    for item in Path(path).rglob("*"):
        try:
            if item.is_file() and not item.is_symlink():  # links point into blobs/, counted there
                total += item.stat().st_size
        except OSError:
            pass
    return total


def list_models(models_dir: Path) -> list[LocalModel]:
    result = []
    for name, repo in REPOS.items():
        snapshot = _snapshot(models_dir, repo)
        size = folder_size(_cache_dir(models_dir, repo)) if snapshot else 0
        mb, text = INFO[name]
        result.append(LocalModel(name, repo, snapshot, size, mb, text))
    return result


def is_downloaded(models_dir: Path, name: str) -> bool:
    repo = REPOS.get(name)
    return bool(repo) and _snapshot(models_dir, repo) is not None


def delete_model(models_dir: Path, name: str) -> bool:
    repo = REPOS.get(name)
    if not repo:
        return False
    folder = _cache_dir(models_dir, repo)
    if not folder.exists():
        return False
    shutil.rmtree(folder, ignore_errors=True)
    log.info("Deleted speech model %s", name)
    return not folder.exists()


def download_model(models_dir: Path, name: str, progress: Callable[[float, str], None] | None = None,
                   cancel=None) -> Path:
    """Download ``name`` into ``models_dir`` (Hugging Face cache layout).

    Progress is estimated from the bytes on disk against the known size."""
    import threading

    repo = REPOS[name]
    progress = progress or (lambda *_a: None)
    expected = INFO[name][0] * 1024 * 1024
    progress(0.0, f"Downloading {name} (about {INFO[name][0]} MB)…")
    from huggingface_hub import snapshot_download  # installed with faster-whisper

    Path(models_dir).mkdir(parents=True, exist_ok=True)
    if cancel is not None and cancel.is_set():
        raise InterruptedError("cancelled")
    done = threading.Event()

    def watch() -> None:
        cache = _cache_dir(models_dir, repo)
        while not done.wait(0.5):
            have = folder_size(cache) if cache.exists() else 0
            progress(min(0.99, have / expected), f"Downloading {name}: {have // (1024 * 1024)} of "
                                                 f"~{INFO[name][0]} MB")

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        path = snapshot_download(
            repo,
            cache_dir=str(models_dir),
            allow_patterns=["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json",
                            "vocabulary.*"],
        )
    finally:
        done.set()
        watcher.join(timeout=2)
    progress(1.0, f"{name} is ready")
    log.info("Downloaded speech model %s to %s", name, path)
    return Path(path)


def recommended_model(has_gpu: bool) -> str:
    return "large-v3-turbo" if has_gpu else "small"
