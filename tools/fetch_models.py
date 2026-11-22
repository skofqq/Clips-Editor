"""Download the models the installer carries into build/models/<component>/.

Each component folder mirrors %LOCALAPPDATA%\\TwitchShortsStudio\\models, so the
installer copies it there as is. Speech models keep the Hugging Face cache
layout the app reads (models--org--name/snapshots/<rev>/model.bin, refs/main),
with links replaced by real files so the installer can pack them.

    python tools/fetch_models.py [--out build/models] [speech model names...]
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_SPEECH = ("large-v3-russian-int8", "small", "gigaam-v3", "parakeet-v3")
EXTRA = ("separation", "speakers", "face")


def component_name(model: str) -> str:
    """Inno Setup component / folder name for a speech model: letters, digits and _ only."""
    return "speech_" + "".join(ch if ch.isalnum() else "_" for ch in model)


def flatten_cache(models_dir: Path) -> None:
    """Replace links in snapshots/ with the files they point to and drop blobs/."""
    for repo in models_dir.glob("models--*"):
        for item in sorted(repo.glob("snapshots/*/**/*")):
            if item.is_symlink():
                target = item.resolve()
                item.unlink()
                shutil.copy2(target, item)
        shutil.rmtree(repo / "blobs", ignore_errors=True)
        shutil.rmtree(repo / ".no_exist", ignore_errors=True)
    for lock in models_dir.rglob("*.lock"):
        lock.unlink(missing_ok=True)
    shutil.rmtree(models_dir / ".locks", ignore_errors=True)


def fetch_onnx(out: Path, name: str) -> Path:
    from core.transcription import onnx_engine

    folder = out / component_name(name)
    if not onnx_engine.is_downloaded(folder, name):
        onnx_engine.download(folder, name, lambda value, text: print(f"  {text}", flush=True))
    shutil.rmtree(onnx_engine.model_dir(folder, name) / ".cache", ignore_errors=True)
    return folder


def fetch_speech(out: Path, name: str) -> Path:
    from core.transcription import onnx_engine
    from core.transcription.models import REPOS, download_model, is_downloaded

    if onnx_engine.is_onnx_model(name):
        return fetch_onnx(out, name)
    if name not in REPOS:
        raise SystemExit(f"Unknown speech model {name!r}. Known: {', '.join(REPOS)}")
    folder = out / component_name(name)
    if not is_downloaded(folder, name):
        last = [-1]

        def progress(value: float, text: str) -> None:
            step = int(value * 10)
            if step != last[0]:
                last[0] = step
                print(f"  {text}", flush=True)

        download_model(folder, name, progress)
    flatten_cache(folder)
    return folder


def fetch_extra(out: Path, kind: str) -> Path:
    folder = out / kind
    if kind == "separation":
        from core.audio.separation import download_model
    elif kind == "speakers":
        from core.transcription.diarization import download_model, model_path

        if model_path(folder).is_file():
            return folder
    else:
        from core.vision.emotion import download_model as download_emotion
        from core.vision.faces import download_model

        download_emotion(folder)  # Auto cover reads emotions next to the face model
    download_model(folder)
    return folder


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("models", nargs="*", default=list(DEFAULT_SPEECH), help="speech models to bundle")
    parser.add_argument("--out", type=Path, default=ROOT / "build" / "models")
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    for name in args.models:
        print(f"Speech model {name}...", flush=True)
        fetch_speech(args.out, name)
    for kind in EXTRA:
        print(f"Model: {kind}...", flush=True)
        fetch_extra(args.out, kind)
    print("Models are in", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
