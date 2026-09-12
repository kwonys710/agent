"""Runs only where FFmpeg is installed — these need a real video file."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from dailyreels.manifest import read_manifest
from dailyreels.cli import main
from dailyreels.scanner import scan
from dailyreels.probe import run_ffprobe

pytestmark = pytest.mark.skipif(
    shutil.which("ffprobe") is None or shutil.which("ffmpeg") is None,
    reason="FFmpeg/ffprobe not installed in this environment",
)


@pytest.fixture
def sample_video(tmp_path: Path) -> Path:
    path = tmp_path / "IMG_001.mp4"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=1080x1920:rate=30:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
        ],
        check=True,
    )
    return path


def test_run_ffprobe_reads_a_real_file(sample_video):
    payload = run_ffprobe(sample_video)
    assert payload["streams"][0]["codec_type"] == "video"


def test_scan_end_to_end(make_settings, sample_video, tmp_path, capsys):
    settings = make_settings()
    manifest = scan(settings)
    assert len(manifest.videos) == 1
    video = manifest.videos[0]
    assert video.orientation == "portrait"
    assert 1.5 < video.duration < 2.5
    assert video.fps == 30.0


def test_cli_scan_writes_manifest(tmp_path, sample_video, monkeypatch):
    monkeypatch.delenv("DAILYREELS_ROOT", raising=False)
    assert main(["scan", "--root", str(tmp_path)]) == 0
    payload = read_manifest(tmp_path / "data" / "manifest.json")
    assert payload["summary"]["video_count"] == 1
