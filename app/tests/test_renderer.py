from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from dailyreels import renderer
from dailyreels.edit_plan import RenderClip, RenderPlan
from dailyreels.fonts import FontChoice
from dailyreels.renderer import (
    RenderError,
    build_video_filter,
    escape_filter_path,
    next_output_path,
    render,
    segment_command,
    validate_output,
)


def make_clip(**overrides) -> RenderClip:
    values = dict(
        clip_id="clip_001", file="clip_001.mp4", path=Path("/videos/clip_001.mp4"),
        source_duration=10.0, use_duration=2.0, start=4.0,
        time_label="07:50", caption="이른 아침 이동 중.", scene="commute", is_hook=False,
    )
    values.update(overrides)
    return RenderClip(**values)


def test_next_output_path_versions_instead_of_overwriting(tmp_path):
    first = next_output_path(tmp_path, "2026-09-12")
    assert first.name == "DailyReel_2026-09-12.mp4"

    first.write_bytes(b"x")
    second = next_output_path(tmp_path, "2026-09-12")
    assert second.name == "DailyReel_2026-09-12_v02.mp4"

    second.write_bytes(b"x")
    assert next_output_path(tmp_path, "2026-09-12").name == "DailyReel_2026-09-12_v03.mp4"


def test_video_filter_fills_without_stretching_and_never_rotates():
    chain = build_video_filter(1080, 1920, 30, "seg_001.ass", Path("/usr/share/fonts"))
    assert "scale=1080:1920:force_original_aspect_ratio=increase" in chain
    assert "crop=1080:1920" in chain
    assert "setsar=1" in chain and "fps=30" in chain
    assert "subtitles=seg_001.ass" in chain
    assert "fontsdir='/usr/share/fonts'" in chain
    assert "transpose" not in chain and "rotate" not in chain  # FFmpeg autorotation only


def test_escape_filter_path_handles_windows_drives():
    assert escape_filter_path(r"C:\Windows\Fonts") == r"C\:/Windows/Fonts"


def test_segment_command_trims_and_mutes(make_settings, tmp_path):
    settings = make_settings(render={"audio_mode": "mute", "crf": 20})
    command = segment_command(settings, make_clip(), "seg_001.ass", "seg_001.mp4", None)

    assert command[0] == settings.ffmpeg
    assert command[command.index("-ss") + 1] == "4.000"
    assert command[command.index("-t") + 1] == "2.000"
    assert command.index("-ss") < command.index("-i")  # seek before decode
    assert "-an" in command
    assert command[command.index("-c:v") + 1] == "libx264"
    assert command[command.index("-pix_fmt") + 1] == "yuv420p"
    assert command[-1] == "seg_001.mp4"


def test_segment_command_keeps_audio_when_asked(make_settings):
    settings = make_settings(render={"audio_mode": "original"})
    command = segment_command(settings, make_clip(), "a.ass", "a.mp4", None)
    assert "-an" not in command and "aac" in command


def test_run_ffmpeg_surfaces_stderr(tmp_path):
    with pytest.raises(RenderError, match="not found"):
        renderer.run_ffmpeg(["ffmpeg_does_not_exist", "-i", "x"])

    with pytest.raises(RenderError, match="exit"):
        renderer.run_ffmpeg(["python3", "-c", "import sys;sys.stderr.write('boom');sys.exit(3)"])


def _plan() -> RenderPlan:
    return RenderPlan(date="2026-09-12", story="s", target_duration=4.0, clips=[make_clip()])


def test_validate_output_rejects_wrong_size_and_missing_file(make_settings, tmp_path, monkeypatch):
    settings = make_settings()
    target = tmp_path / "out.mp4"

    with pytest.raises(RenderError, match="does not exist"):
        validate_output(settings, target, _plan())

    target.write_bytes(b"x")
    payload = {"streams": [{"codec_type": "video", "codec_name": "h264", "width": 1920,
                            "height": 1080, "avg_frame_rate": "30/1", "duration": "2.0"}],
               "format": {"duration": "2.0"}}
    monkeypatch.setattr(renderer.probe_mod, "run_ffprobe", lambda *a, **k: payload)
    with pytest.raises(RenderError, match="1920x1080, expected 1080x1920"):
        validate_output(settings, target, _plan())


def test_validate_output_accepts_a_correct_reel(make_settings, tmp_path, monkeypatch):
    settings = make_settings()
    target = tmp_path / "out.mp4"
    target.write_bytes(b"x" * 2048)
    payload = {"streams": [{"codec_type": "video", "codec_name": "h264", "width": 1080,
                            "height": 1920, "avg_frame_rate": "30/1", "duration": "2.0"}],
               "format": {"duration": "2.0"}}
    monkeypatch.setattr(renderer.probe_mod, "run_ffprobe", lambda *a, **k: payload)

    stats = validate_output(settings, target, _plan())
    assert stats == {"width": 1080, "height": 1920, "duration": 2.0, "fps": 30.0,
                     "codec": "h264", "file_size": 2048}


def _fake_plan_file(root: Path) -> Path:
    (root / "clip_001.mp4").write_bytes(b"source-one")
    (root / "clip_002.mp4").write_bytes(b"source-two")
    plan = {
        "date": "2026-09-12",
        "story": "식사와 도서관 방문으로 채운 하루",
        "hook": {"file": "clip_002.mp4", "caption": "철판에 골고루 볶는 밥.",
                 "duration": 2.0, "source_duration": 10.0, "id": "clip_002"},
        "clips": [{"id": "clip_001", "file": "clip_001.mp4", "time": "07:50",
                   "caption": "이른 아침 이동 중.", "duration": 2.0, "source_duration": 10.0}],
    }
    path = root / "data" / "edit_plan.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    return path


def _stub_ffmpeg(monkeypatch, calls: list):
    def fake_run(command, cwd=None, timeout=1800):
        calls.append((list(map(str, command)), cwd))
        target = Path(str(command[-1]))
        if not target.is_absolute() and cwd:
            target = Path(cwd) / target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"rendered" * 256)

    monkeypatch.setattr(renderer, "run_ffmpeg", fake_run)
    monkeypatch.setattr(
        renderer.probe_mod, "run_ffprobe",
        lambda *a, **k: {"streams": [{"codec_type": "video", "codec_name": "h264",
                                      "width": 1080, "height": 1920,
                                      "avg_frame_rate": "30/1", "duration": "4.0"}],
                         "format": {"duration": "4.0"}},
    )
    monkeypatch.setattr(renderer, "resolve_font",
                        lambda style: FontChoice(name="Paperlogy", file=None, source="test"))


def test_render_orders_hook_first_and_cleans_up_temp(make_settings, tmp_path, monkeypatch):
    settings = make_settings()
    _fake_plan_file(tmp_path)
    calls: list = []
    _stub_ffmpeg(monkeypatch, calls)

    result = render(settings, previews=False)

    segment_calls = [c for c in calls if c[0][-1].startswith("seg_")]
    assert len(segment_calls) == 2
    assert "clip_002.mp4" in " ".join(segment_calls[0][0])  # hook renders first
    assert "clip_001.mp4" in " ".join(segment_calls[1][0])
    assert result.output.name == "DailyReel_2026-09-12.mp4"
    assert result.plan.hook is not None and result.plan.hook.file == "clip_002.mp4"
    assert not (settings.data_dir / "render_tmp").exists()  # temp removed on success


def test_render_keeps_temp_on_failure(make_settings, tmp_path, monkeypatch):
    settings = make_settings()
    _fake_plan_file(tmp_path)
    _stub_ffmpeg(monkeypatch, [])
    monkeypatch.setattr(
        renderer, "run_ffmpeg",
        lambda *a, **k: (_ for _ in ()).throw(RenderError("ffmpeg failed (exit 1):\nboom")),
    )

    with pytest.raises(RenderError, match="boom"):
        render(settings, previews=False)

    temp = settings.data_dir / "render_tmp"
    assert temp.is_dir()
    assert (temp / "seg_001.ass").is_file()  # subtitles kept for debugging


def test_render_never_touches_sources_or_plan(make_settings, tmp_path, monkeypatch):
    settings = make_settings()
    plan_path = _fake_plan_file(tmp_path)
    _stub_ffmpeg(monkeypatch, [])

    def fingerprint() -> dict:
        return {
            path.name: hashlib.md5(path.read_bytes()).hexdigest()
            for path in [tmp_path / "clip_001.mp4", tmp_path / "clip_002.mp4", plan_path]
        }

    before = fingerprint()
    render(settings, previews=False)
    assert fingerprint() == before
    assert (tmp_path / "clip_001.mp4").exists() and plan_path.exists()
