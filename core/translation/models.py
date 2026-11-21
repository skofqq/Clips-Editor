"""Offline translation models: Russian ↔ English.

The models are quickmt (CTranslate2 Transformer models with SentencePiece
tokenizers, CC BY 4.0) from Hugging Face, ~400 MB each. They run with
CTranslate2, which faster-whisper already installs, so translation needs
no PyTorch and works without the internet once downloaded.

Each direction is downloaded once into ``models_dir/translation/<key>``;
a ``.complete`` mark is written when every file is there.
"""

from __future__ import annotations

import logging
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

COMPLETE_MARK = ".complete"
FILES = ("config.json", "model.bin", "src.spm.model", "tgt.spm.model",
         "source_vocabulary.json", "target_vocabulary.json")
Progress = Callable[[float, str], None]


@dataclass(frozen=True)
class TranslationModel:
    key: str  # "ru-en"
    source: str
    target: str
    repo: str
    approx_mb: int

    @property
    def label(self) -> str:
        names = {"ru": "Russian", "en": "English"}
        return f"{names.get(self.source, self.source)} → {names.get(self.target, self.target)}"


MODELS = {
    "ru-en": TranslationModel("ru-en", "ru", "en", "quickmt/quickmt-ru-en", 410),
    "en-ru": TranslationModel("en-ru", "en", "ru", "quickmt/quickmt-en-ru", 400),
}


class TranslationCancelled(Exception):
    pass


def direction_for(language: str) -> str:
    """The direction that makes a second-language version of a ``language`` transcript."""
    return "en-ru" if (language or "").lower().startswith("en") else "ru-en"


def model_dir(models_dir: Path, key: str) -> Path:
    return Path(models_dir) / "translation" / key


def is_downloaded(models_dir: Path, key: str) -> bool:
    folder = model_dir(models_dir, key)
    return (folder / COMPLETE_MARK).exists() and all((folder / f).exists() for f in FILES)


def delete_model(models_dir: Path, key: str) -> bool:
    folder = model_dir(models_dir, key)
    if not folder.exists():
        return False
    shutil.rmtree(folder, ignore_errors=True)
    return not folder.exists()


def _size(folder: Path) -> int:
    return sum(p.stat().st_size for p in folder.rglob("*") if p.is_file()) if folder.exists() else 0


def download_model(models_dir: Path, key: str, progress: Progress | None = None, cancel=None) -> Path:
    """Download the ``key`` model (if needed) and return its folder."""
    model = MODELS[key]
    folder = model_dir(models_dir, key)
    progress = progress or (lambda *_a: None)
    if is_downloaded(models_dir, key):
        return folder
    if cancel is not None and cancel.is_set():
        raise TranslationCancelled()
    from huggingface_hub import snapshot_download  # installed with faster-whisper

    folder.mkdir(parents=True, exist_ok=True)
    expected = model.approx_mb * 1024 * 1024
    progress(0.0, f"Downloading the {model.label} model (about {model.approx_mb} MB, once)…")
    done = threading.Event()

    def watch() -> None:
        while not done.wait(0.5):
            have = _size(folder)
            progress(min(0.99, have / expected),
                     f"Downloading the {model.label} model: {have // (1024 * 1024)} of ~{model.approx_mb} MB")

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        snapshot_download(model.repo, local_dir=str(folder), allow_patterns=list(FILES))
    finally:
        done.set()
        watcher.join(timeout=2)
    missing = [f for f in FILES if not (folder / f).exists()]
    if missing:
        raise RuntimeError(f"The translation model is incomplete (missing {', '.join(missing)}).")
    (folder / COMPLETE_MARK).write_text("ok", encoding="utf-8")
    log.info("Downloaded translation model %s to %s", key, folder)
    return folder
