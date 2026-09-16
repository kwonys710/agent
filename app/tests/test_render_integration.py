"""Real FFmpeg renders. Skipped where FFmpeg is not installed."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from dailyreels.fonts import resolve_font
from dailyreels.renderer import render

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="FFmpeg/ffprobe not installed in this environment",
)


def make_source(
    path: Path, size: str, seconds: float = 6.0, rotate: int | None = None, marker: bool = False
) -> Path:
    """Build a test clip. `rotate` writes a display matrix, like a phone shooting portrait."""
    target = path.with_name(path.stem + "_flat.mp4") if rotate is not None else path
    command = [
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"color=c=0x101820:s={size}:r=30:d={seconds}",
    ]
    if marker:  # white square centred on the source's top edge
        command += ["-vf", "drawbox=x=(iw-360)/2:y=0:w=360:h=360:color=white@1:t=fill"]
    command += ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(target)]
    subprocess.run(command, check=True, capture_output=True)

    if rotate is not None:
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-display_rotation", str(rotate),
             "-i", str(target), "-c", "copy", str(path)],
            check=True, capture_output=True,
        )
        target.unlink()
    return path


def band_max_luma(video: Path, at: float, crop: str) -> int:
    """Brightest pixel inside a region — white captions on a dark frame read ~255."""
    completed = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{at}", "-i", str(video), "-frames:v", "1",
         "-vf", f"crop={crop},format=gray", "-f", "rawvideo", "-"],
        check=True, capture_output=True,
    )
    return max(completed.stdout) if completed.stdout else 0


def first_frame_digest(video: Path) -> str:
    completed = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(video), "-frames:v", "1", "-f", "rawvideo", "-"],
        check=True, capture_output=True,
    )
    return hashlib.md5(completed.stdout).hexdigest()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    make_source(tmp_path / "clip_001.mp4", "1080x1920")            # portrait
    make_source(tmp_path / "clip_002.mp4", "1920x1080", rotate=270)  # phone portrait metadata
    make_source(tmp_path / "clip_003.mp4", "1920x1080")            # true landscape
    plan = {
        "date": "2026-09-12",
        "story": "식사와 도서관 방문으로 채운 하루",
        "target_duration": 6,
        "hook": {"id": "clip_002", "file": "clip_002.mp4", "caption": "철판에 골고루\n볶는 밥.",
                 "duration": 2.0},
        "clips": [
            {"id": "clip_001", "file": "clip_001.mp4", "time": "07:50",
             "caption": "이른 아침 이동 중.", "duration": 2.0},
            {"id": "clip_003", "file": "clip_003.mp4", "time": "13:03",
             "caption": "점심 먹고 도서관.", "duration": 2.0},
        ],
    }
    plan_path = tmp_path / "data" / "edit_plan.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return tmp_path


def test_render_produces_a_valid_portrait_reel(make_settings, project):
    settings = make_settings()
    result = render(settings)

    assert result.output.is_file()
    assert (result.width, result.height) == (1080, 1920)
    assert result.codec == "h264"
    assert result.fps == 30.0
    assert result.duration == pytest.approx(6.0, abs=0.4)
    assert result.file_size > 0
    assert result.plan.hook is not None and result.plan.hook.file == "clip_002.mp4"

    head = result.output.read_bytes()[:65536]
    assert head.index(b"moov") < head.index(b"mdat")  # +faststart

    assert len(result.previews) == 3
    assert all(path.stat().st_size > 0 for path in result.previews)
    assert not (settings.data_dir / "render_tmp").exists()


def test_captions_are_burned_in_at_the_expected_place(make_settings, project):
    settings = make_settings()
    result = render(settings, previews=False)

    # Hook: large text near the middle of the frame, first clip.
    assert band_max_luma(result.output, 1.0, "1080:500:0:710") > 200
    # Body: caption sits above the Instagram UI, not at the very bottom.
    assert band_max_luma(result.output, 3.0, "1080:300:0:1400") > 200
    assert band_max_luma(result.output, 3.0, "1080:120:0:1800") < 60   # bottom stays clear
    assert band_max_luma(result.output, 3.0, "1080:200:0:0") < 60      # top stays clear


def _single_clip_project(root: Path, rotate: int | None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    make_source(root / "clip_001.mp4", "1920x1080", seconds=3.0, rotate=rotate, marker=True)
    plan = {
        "date": "2026-09-12",
        "clips": [{"id": "clip_001", "file": "clip_001.mp4", "time": "", "caption": "",
                   "duration": 1.5}],
    }
    plan_path = root / "data" / "edit_plan.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    return root


def test_rotated_source_is_autorotated_exactly_once(make_settings, tmp_path):
    """A phone clip stored as 1920x1080 + rotation must render asportrait content."""
    plain_root = _single_clip_project(tmp_path / "plain", rotate=None)
    rotated_root = _single_clip_project(tmp_path / "rotated", rotate=270)

    plain = render(make_settings(root=plain_root), previews=False)
    rotated = render(make_settings(root=rotated_root), previews=False)

    assert (plain.width, plain.height) == (rotated.width, rotated.height) == (1080, 1920)

    corner = "360:360:360:0"  # top centre of the rendered frame
    plain_corner = band_max_luma(plain.output, 0.5, corner)
    rotated_corner = band_max_luma(rotated.output, 0.5, corner)
    # Landscape source keeps the marker on top; autorotation moves it to a side.
    assert plain_corner > 200
    assert rotated_corner < 60
    assert first_frame_digest(plain.output) != first_frame_digest(rotated.output)


def test_output_versioning_does_not_overwrite(make_settings, project):
    settings = make_settings()
    first = render(settings, previews=False).output
    second = render(settings, previews=False).output

    assert first.name == "DailyReel_2026-09-12.mp4"
    assert second.name == "DailyReel_2026-09-12_v02.mp4"
    assert first.is_file() and first.stat().st_size > 0


def test_sources_and_plan_are_untouched(make_settings, project):
    settings = make_settings()
    watched = sorted(project.glob("*.mp4")) + [project / "data" / "edit_plan.json"]
    before = {path.name: (path.stat().st_size, hashlib.md5(path.read_bytes()).hexdigest())
              for path in watched}

    render(settings, previews=False)

    after = {path.name: (path.stat().st_size, hashlib.md5(path.read_bytes()).hexdigest())
             for path in watched}
    assert after == before
    assert sorted(p.name for p in project.glob("*.mp4")) == [
        "clip_001.mp4", "clip_002.mp4", "clip_003.mp4"
    ]


def test_resolved_font_can_draw_korean(make_settings, project):
    font = resolve_font(make_settings().caption_style)
    assert font.name
    # A missing glyph renders as a box, so compare a Korean caption against an empty one.
    settings = make_settings()
    result = render(settings, previews=False)
    assert band_max_luma(result.output, 3.0, "1080:300:0:1400") > 200
