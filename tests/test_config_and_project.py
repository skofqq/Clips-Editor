from __future__ import annotations

import json
import sys

import pytest

from app.config import AppConfig
from core.media.errors import FFmpegNotFoundError
from core.media.ffmpeg_locator import locate_ffmpeg
from core.projects.project import (
    MediaRef,
    Project,
    ProjectFormatError,
    load_project,
    save_project,
)


def test_config_roundtrip(data_dir):
    config = AppConfig.load()
    assert config.data_dir == data_dir
    config.ffmpeg_dir = r"C:\tools\ffmpeg"
    for i in range(20):
        config.add_recent(f"clip{i}.mp4")
    config.add_recent("clip5.mp4")
    config.save()

    loaded = AppConfig.load()
    assert loaded.ffmpeg_dir == r"C:\tools\ffmpeg"
    assert loaded.recent_files[0] == "clip5.mp4"
    assert len(loaded.recent_files) == 15
    assert loaded.recent_files.count("clip5.mp4") == 1


def test_config_survives_corrupt_file(data_dir):
    data_dir.mkdir(parents=True)
    (data_dir / "config.json").write_text("{not json", encoding="utf-8")
    assert AppConfig.load().ffmpeg_dir == ""


def test_locator_prefers_configured_dir(tmp_path):
    exe = ".exe" if sys.platform == "win32" else ""
    bin_dir = tmp_path / "custom" / "bin"
    bin_dir.mkdir(parents=True)
    for name in ("ffmpeg", "ffprobe"):
        (bin_dir / f"{name}{exe}").write_text("")
    paths = locate_ffmpeg(tmp_path / "custom", bundled=tmp_path / "none", search_path=False)
    assert paths.ffmpeg.parent == bin_dir


def test_locator_reports_missing(tmp_path):
    with pytest.raises(FFmpegNotFoundError):
        locate_ffmpeg(None, bundled=tmp_path, search_path=False)


def test_project_roundtrip_keeps_unknown_keys(tmp_path):
    project = Project(name="Мой клип")
    assert project.add_media(MediaRef(path=str(tmp_path / "a.mp4"), size_bytes=10, duration_s=1.5))
    assert not project.add_media(MediaRef(path=str(tmp_path / "a.mp4")))
    project.extra["future_feature"] = {"layout": "stacked"}

    saved = save_project(project, tmp_path / "proj")
    assert saved.suffix == ".tssproj"
    raw = json.loads(saved.read_text(encoding="utf-8"))
    assert raw["schema_version"] == 1

    loaded = load_project(saved)
    assert loaded.name == "Мой клип"
    assert loaded.media[0].duration_s == 1.5
    assert loaded.extra["future_feature"] == {"layout": "stacked"}


@pytest.mark.parametrize(
    "content",
    ["[]", "{}", '{"schema_version": 999}', '{"schema_version": 1, "media": [{}]}', "garbage"],
)
def test_project_rejects_bad_files(tmp_path, content):
    path = tmp_path / "bad.tssproj"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ProjectFormatError):
        load_project(path)
