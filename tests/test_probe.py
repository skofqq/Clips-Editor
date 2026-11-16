from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import pytest

from core.media.errors import ProbeError
from core.media.probe import (
    format_duration,
    format_fps,
    format_size,
    parse_probe_output,
    probe_media,
)

TWITCH_LIKE = {
    "streams": [
        {
            "index": 0, "codec_type": "video", "codec_name": "h264",
            "width": 1920, "height": 1080, "pix_fmt": "yuv420p",
            "r_frame_rate": "60/1", "avg_frame_rate": "60/1", "time_base": "1/15360",
            "duration": "29.983333", "nb_frames": "1799",
        },
        {
            "index": 1, "codec_type": "audio", "codec_name": "aac",
            "sample_rate": "48000", "channels": 2, "channel_layout": "stereo",
            "duration": "30.016000", "tags": {"language": "und"},
        },
    ],
    "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "30.016000",
               "size": "15000000", "bit_rate": "3997868"},
}


def test_parse_twitch_clip():
    info = parse_probe_output(TWITCH_LIKE, Path("clip.mp4"))
    assert info.video is not None
    assert (info.video.width, info.video.height) == (1920, 1080)
    assert info.video.fps == Fraction(60)
    assert info.video.time_base == Fraction(1, 15360)
    assert info.video.frame_count == 1799
    assert not info.video.is_vfr_suspected
    assert info.duration_s == pytest.approx(30.016)
    assert info.size_bytes == 15_000_000
    assert len(info.audio) == 1
    assert info.audio[0].sample_rate == 48000
    assert info.audio[0].channel_layout == "stereo"


def test_ntsc_rate_and_vfr_detection():
    data = {
        "streams": [{
            "index": 0, "codec_type": "video", "codec_name": "h264", "width": 1280, "height": 720,
            "r_frame_rate": "30000/1001", "avg_frame_rate": "24000/1001",
        }],
        "format": {"duration": "5"},
    }
    info = parse_probe_output(data, Path("x.mkv"))
    assert info.video.fps == Fraction(30000, 1001)
    assert info.video.is_vfr_suspected
    assert not info.has_audio


def test_rotation_from_side_data():
    data = {
        "streams": [{
            "index": 0, "codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080,
            "r_frame_rate": "30/1", "avg_frame_rate": "30/1",
            "side_data_list": [{"side_data_type": "Display Matrix", "rotation": -90}],
        }],
        "format": {"duration": "1"},
    }
    info = parse_probe_output(data, Path("phone.mp4"))
    assert info.video.rotation == 270
    assert info.video.display_size == (1080, 1920)


def test_multiple_audio_streams_and_cover_art_skipped():
    data = {
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "mjpeg", "width": 300, "height": 300,
             "disposition": {"attached_pic": 1}},
            {"index": 1, "codec_type": "video", "codec_name": "hevc", "width": 2560, "height": 1440,
             "r_frame_rate": "60/1", "avg_frame_rate": "60/1"},
            {"index": 2, "codec_type": "audio", "codec_name": "aac", "sample_rate": "48000", "channels": 2},
            {"index": 3, "codec_type": "audio", "codec_name": "opus", "sample_rate": "48000", "channels": 1},
        ],
        "format": {"duration": "10"},
    }
    info = parse_probe_output(data, Path("multi.mkv"))
    assert info.video.codec == "hevc"
    assert [a.codec for a in info.audio] == ["aac", "opus"]


def test_bad_values_do_not_crash():
    data = {
        "streams": [{"index": 0, "codec_type": "video", "codec_name": "h264",
                     "r_frame_rate": "0/0", "avg_frame_rate": "N/A", "width": "?"}],
        "format": {"duration": "N/A"},
    }
    info = parse_probe_output(data, Path("odd.ts"))
    assert info.video.fps == 0
    assert info.video.width == 0
    assert info.duration_s == 0.0
    assert not info.video.is_vfr_suspected


def test_no_streams_is_an_error():
    with pytest.raises(ProbeError):
        parse_probe_output({"streams": [], "format": {}}, Path("empty.mp4"))


def test_formatters():
    assert format_duration(0) == "00:00.000"
    assert format_duration(61.5) == "01:01.500"
    assert format_duration(3725.004) == "1:02:05.004"
    assert format_size(512) == "512 B"
    assert format_size(15_000_000) == "14.3 MB"
    assert format_fps(Fraction(30000, 1001)) == "29.97"
    assert format_fps(Fraction(60)) == "60"


# ---- real ffprobe ---------------------------------------------------------

def test_probe_real_mp4(sample_clips, ffmpeg_paths):
    path = sample_clips["mp4"]
    before = path.stat().st_mtime_ns, path.stat().st_size
    info = probe_media(path, ffmpeg_paths.ffprobe)
    assert info.video.width == 1280 and info.video.height == 720
    assert info.video.fps == 30
    assert info.audio and info.audio[0].sample_rate == 48000
    assert info.duration_s == pytest.approx(2.0, abs=0.1)
    # Source file must be untouched.
    assert (path.stat().st_mtime_ns, path.stat().st_size) == before


def test_probe_real_mkv_without_audio(sample_clips, ffmpeg_paths):
    info = probe_media(sample_clips["mkv_noaudio"], ffmpeg_paths.ffprobe)
    assert info.video.fps == 60
    assert not info.has_audio
    assert "matroska" in info.container


def test_probe_missing_and_invalid_files(tmp_path, ffmpeg_paths):
    with pytest.raises(ProbeError):
        probe_media(tmp_path / "nope.mp4", ffmpeg_paths.ffprobe)
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"this is not a video")
    with pytest.raises(ProbeError):
        probe_media(junk, ffmpeg_paths.ffprobe)
