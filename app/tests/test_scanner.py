from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from dailyreels import probe
from dailyreels.scanner import build_video_meta, find_videos, scan
from tests.conftest import probe_payload


def touch(path: Path, content: bytes = b"video-bytes") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_find_videos_only_looks_at_the_root(make_settings, tmp_path):
    settings = make_settings()
    touch(tmp_path / "IMG_001.mp4")
    touch(tmp_path / "IMG_002.MOV")
    touch(tmp_path / "IMG_003.m4v")
    touch(tmp_path / "출근길.mp4")          # Korean filename
    touch(tmp_path / "notes.txt")
    touch(tmp_path / ".hidden.mp4")
    touch(tmp_path / "output" / "DailyReel_2026-09-12.mp4")   # excluded folder
    touch(tmp_path / "app" / "nested.mp4")
    touch(tmp_path / "data" / "old.mov")

    names = [path.name for path in find_videos(settings)]
    assert names == ["IMG_001.mp4", "IMG_002.MOV", "IMG_003.m4v", "출근길.mp4"]


def test_build_video_meta_uses_creation_time(make_settings, tmp_path):
    settings = make_settings()
    path = touch(tmp_path / "IMG_001.mp4")
    meta = build_video_meta(path, probe_payload(), settings.root)

    assert meta.file == "IMG_001.mp4"
    assert meta.captured_at_source == "creation_time"
    assert meta.captured_at.isoformat(timespec="seconds").startswith("2026-09-12T")
    assert (meta.width, meta.height, meta.orientation) == (1080, 1920, "portrait")
    assert meta.fps == 29.97
    assert meta.duration == 9.512
    assert meta.codec == "h264"
    assert meta.file_size == len(b"video-bytes")


def test_build_video_meta_falls_back_to_mtime_and_records_source(make_settings, tmp_path):
    settings = make_settings()
    path = touch(tmp_path / "IMG_002.mp4")
    meta = build_video_meta(path, probe_payload(creation_time=None), settings.root)

    assert meta.captured_at_source == "file_mtime"
    assert meta.captured_at.year >= 2020


def test_build_video_meta_rejects_file_without_video_stream(make_settings, tmp_path):
    settings = make_settings()
    path = touch(tmp_path / "IMG_003.mp4")
    with pytest.raises(probe.FFprobeError, match="no video stream"):
        build_video_meta(path, probe_payload(codec_type="audio"), settings.root)


def test_scan_sorts_by_capture_time_and_keeps_going_after_a_bad_file(
    make_settings, tmp_path
):
    settings = make_settings()
    touch(tmp_path / "IMG_A.mp4")
    touch(tmp_path / "IMG_B.mp4")
    touch(tmp_path / "IMG_C.mp4")

    times = {
        "IMG_A.mp4": "2026-09-12T23:41:00.000000Z",
        "IMG_B.mp4": "2026-09-12T07:03:00.000000Z",
    }

    def fake_prober(path: Path) -> dict:
        if path.name == "IMG_C.mp4":
            raise probe.FFprobeError("moov atom not found")
        return probe_payload(creation_time=times[path.name])

    manifest = scan(settings, prober=fake_prober)

    assert [video.file for video in manifest.videos] == ["IMG_B.mp4", "IMG_A.mp4"]
    assert [error.file for error in manifest.errors] == ["IMG_C.mp4"]
    assert manifest.summary()["video_count"] == 2
    assert manifest.summary()["portrait"] == 2


def test_scan_never_touches_the_source_files(make_settings, tmp_path):
    settings = make_settings()
    path = touch(tmp_path / "IMG_001.mp4")
    before = (path.stat().st_size, path.stat().st_mtime_ns, hashlib.md5(path.read_bytes()).hexdigest())

    scan(settings, prober=lambda p: probe_payload())

    after = (path.stat().st_size, path.stat().st_mtime_ns, hashlib.md5(path.read_bytes()).hexdigest())
    assert before == after
    assert [p.name for p in tmp_path.iterdir() if p.is_file()] == ["IMG_001.mp4"]
