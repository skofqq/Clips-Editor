import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import fetch_models  # noqa: E402

from core.transcription.models import is_downloaded  # noqa: E402


def test_component_names_match_installer():
    iss = (ROOT / "packaging" / "installer.iss").read_text(encoding="utf-8")
    for name in fetch_models.DEFAULT_SPEECH + ("large-v3-turbo", "large-v3-russian"):
        folder = fetch_models.component_name(name)
        assert f'Models + "{folder}"' in iss
    for kind in fetch_models.EXTRA:
        assert f'Models + "{kind}"' in iss


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symlinks")
def test_flatten_cache_keeps_the_layout_the_app_reads(tmp_path):
    repo = tmp_path / "models--Systran--faster-whisper-small"
    blob = repo / "blobs" / "abc"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"weights")
    snap = repo / "snapshots" / "rev1"
    snap.mkdir(parents=True)
    try:
        (snap / "model.bin").symlink_to(blob)
    except OSError:
        pytest.skip("symlinks not allowed")
    (repo / "refs").mkdir()
    (repo / "refs" / "main").write_text("rev1")
    (tmp_path / ".locks").mkdir()
    fetch_models.flatten_cache(tmp_path)
    assert not (snap / "model.bin").is_symlink()
    assert (snap / "model.bin").read_bytes() == b"weights"
    assert not (repo / "blobs").exists() and not (tmp_path / ".locks").exists()
    assert is_downloaded(tmp_path, "small")
