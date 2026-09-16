from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from dailyreels import probe as probe_mod
from dailyreels.config import Settings
from dailyreels.edit_plan import (
    EditPlanError,
    RenderClip,
    RenderPlan,
    build_render_plan,
    load_edit_plan,
)
from dailyreels.fonts import FontChoice, resolve_font
from dailyreels.subtitles import CaptionStyle, body_text, build_ass, hook_text

log = logging.getLogger(__name__)

TEMP_DIRNAME = "render_tmp"
ANALYSIS_FILENAME = "analysis.json"
PREVIEW_DIRNAME = "preview"
DURATION_TOLERANCE = 0.75  # seconds, concat/keyframe slack


class RenderError(RuntimeError):
    """FFmpeg failed, or the file it produced is not a valid Reel."""


@dataclass
class RenderResult:
    output: Path
    plan: RenderPlan
    width: int
    height: int
    duration: float
    fps: float
    codec: str
    file_size: int
    font: FontChoice
    previews: list[Path] = field(default_factory=list)
    temp_dir: Path | None = None


def load_clip_index(data_dir: Path) -> dict[str, str]:
    """clip_id -> source filename, from v0.3 analysis.json. Read-only, no AI calls."""
    import json

    path = data_dir / ANALYSIS_FILENAME
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        log.warning("could not read %s: %s", path, exc)
        return {}
    index: dict[str, str] = {}
    for entry in payload.get("clips", []) or []:
        if isinstance(entry, dict) and entry.get("clip_id") and entry.get("file"):
            index[str(entry["clip_id"])] = str(entry["file"])
    return index


def _setting(settings: Settings, key: str, default: Any) -> Any:
    value = settings.render.get(key)
    return default if value is None else value


def run_ffmpeg(command: Sequence[str], cwd: Path | None = None, timeout: int = 1800) -> None:
    """Run FFmpeg without a shell; surface its stderr when it fails."""
    log.debug("ffmpeg: %s", " ".join(str(part) for part in command))
    try:
        completed = subprocess.run(
            [str(part) for part in command],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RenderError(
            f"ffmpeg not found ({command[0]}). Install FFmpeg or set DAILYREELS_FFMPEG."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RenderError(f"ffmpeg timed out after {timeout}s") from exc

    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        tail = "\n".join(stderr.splitlines()[-12:]) or "no stderr output"
        raise RenderError(f"ffmpeg failed (exit {completed.returncode}):\n{tail}")


def escape_filter_path(path: Path | str) -> str:
    """Escape a path for use inside an FFmpeg filter argument (Windows drive letters included)."""
    text = str(path).replace("\\", "/")
    return text.replace(":", r"\:").replace("'", r"\'").replace("[", r"\[").replace("]", r"\]")


def next_output_path(output_dir: Path, date: str, template: str = "DailyReel_{date}.mp4") -> Path:
    """Never overwrite an existing Reel: _v02, _v03, ..."""
    base = template.format(date=date)
    candidate = output_dir / base
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    for version in range(2, 100):
        versioned = output_dir / f"{stem}_v{version:02d}{suffix}"
        if not versioned.exists():
            return versioned
    raise RenderError(f"too many existing versions of {base}")


def build_video_filter(width: int, height: int, fps: int, ass_name: str, fonts_dir: Path | None) -> str:
    """Fill the 9:16 frame without stretching, then burn in the caption.

    FFmpeg auto-rotates on decode, so no rotation filter is applied here.
    """
    subtitles = f"subtitles={ass_name}"  # run with cwd=temp dir -> no path escaping
    if fonts_dir is not None:
        subtitles += f":fontsdir='{escape_filter_path(fonts_dir)}'"
    return ",".join(
        [
            f"scale={width}:{height}:force_original_aspect_ratio=increase",
            f"crop={width}:{height}",
            "setsar=1",
            f"fps={fps}",
            subtitles,
            "format=yuv420p",
        ]
    )


def segment_command(
    settings: Settings,
    clip: RenderClip,
    ass_name: str,
    out_name: str,
    fonts_dir: Path | None,
) -> list[str]:
    width = int(_setting(settings, "width", 1080))
    height = int(_setting(settings, "height", 1920))
    fps = int(_setting(settings, "fps", 30))
    command = [
        settings.ffmpeg, "-hide_banner", "-v", "error", "-y",
        "-ss", f"{clip.start:.3f}",
        "-i", str(clip.path),
        "-t", f"{clip.use_duration:.3f}",
        "-vf", build_video_filter(width, height, fps, ass_name, fonts_dir),
        "-c:v", str(_setting(settings, "video_codec", "libx264")),
        "-preset", str(_setting(settings, "preset", "medium")),
        "-crf", str(_setting(settings, "crf", 20)),
        "-pix_fmt", "yuv420p",
        "-r", str(fps),
        "-video_track_timescale", "90000",
    ]
    if str(_setting(settings, "audio_mode", "mute")).lower() == "mute":
        command.append("-an")
    else:  # keep the source audio, normalised to a common format for concat
        command += ["-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2"]
    command.append(out_name)
    return command


def _write_segment_subtitles(clip: RenderClip, style: CaptionStyle, settings: Settings, path: Path, font: FontChoice) -> None:
    text = hook_text(clip.caption, style) if clip.is_hook else body_text(clip.time_label, clip.caption, style)
    content = build_ass(
        text=text,
        duration=clip.use_duration,
        style=style,
        font_name=font.name,
        width=int(_setting(settings, "width", 1080)),
        height=int(_setting(settings, "height", 1920)),
        is_hook=clip.is_hook,
    )
    path.write_text(content, encoding="utf-8")


def validate_output(settings: Settings, path: Path, plan: RenderPlan) -> dict[str, Any]:
    """Exit code 0 is not proof: re-probe the file we just wrote."""
    if not path.is_file():
        raise RenderError(f"render finished but {path} does not exist")
    size = path.stat().st_size
    if size <= 0:
        raise RenderError(f"{path.name} is empty")

    payload = probe_mod.run_ffprobe(path, ffprobe=settings.ffprobe)
    stream = probe_mod.video_stream(payload)
    if stream is None:
        raise RenderError(f"{path.name} has no video stream")

    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)
    duration = probe_mod.parse_duration(payload, stream)
    codec = str(stream.get("codec_name") or "")
    fps = probe_mod.parse_fps(stream)

    expected_w = int(_setting(settings, "width", 1080))
    expected_h = int(_setting(settings, "height", 1920))
    if (width, height) != (expected_w, expected_h):
        raise RenderError(f"{path.name} is {width}x{height}, expected {expected_w}x{expected_h}")
    if duration <= 0:
        raise RenderError(f"{path.name} has zero duration")
    if codec not in ("h264", "hevc"):
        raise RenderError(f"{path.name} has unexpected video codec: {codec or 'unknown'}")

    planned = plan.total_duration
    if planned > 0 and abs(duration - planned) > DURATION_TOLERANCE:
        log.warning("rendered duration %.2fs differs from plan %.2fs", duration, planned)

    return {
        "width": width, "height": height, "duration": duration,
        "fps": fps, "codec": codec, "file_size": size,
    }


def make_previews(settings: Settings, video: Path, duration: float, preview_dir: Path) -> list[Path]:
    """Three stills for human QC. Best effort — never fails the render."""
    preview_dir.mkdir(parents=True, exist_ok=True)
    marks = {
        "preview_start.jpg": min(0.5, duration / 4),
        "preview_middle.jpg": duration / 2,
        "preview_end.jpg": max(0.0, duration - 0.5),
    }
    made: list[Path] = []
    for name, position in marks.items():
        target = preview_dir / name
        try:
            run_ffmpeg(
                [
                    settings.ffmpeg, "-hide_banner", "-v", "error", "-y",
                    "-ss", f"{position:.3f}", "-i", str(video),
                    "-frames:v", "1", "-q:v", "3", str(target),
                ],
                timeout=120,
            )
            made.append(target)
        except RenderError as exc:
            log.warning("preview %s failed: %s", name, exc)
    return made


def render(
    settings: Settings,
    plan_path: Path | None = None,
    keep_temp: bool = False,
    previews: bool = True,
) -> RenderResult:
    """edit_plan.json + source videos + FFmpeg -> one Reel. No AI calls anywhere here."""
    plan_file = plan_path or (settings.data_dir / "edit_plan.json")
    raw_plan = load_edit_plan(plan_file)

    def duration_of(path: Path) -> float:
        payload = probe_mod.run_ffprobe(path, ffprobe=settings.ffprobe)
        stream = probe_mod.video_stream(payload)
        return probe_mod.parse_duration(payload, stream or {})

    plan = build_render_plan(
        raw_plan,
        root=settings.root,
        trim_mode=str(_setting(settings, "trim_mode", "center")),
        duration_of=duration_of,
        clip_files=load_clip_index(settings.data_dir),
    )

    style = CaptionStyle.from_config(settings.caption_style)
    font = resolve_font(settings.caption_style)
    log.info("caption font: %s (%s)", font.name, font.file or "by name")

    temp_dir = settings.data_dir / TEMP_DIRNAME
    if temp_dir.exists():
        shutil.rmtree(temp_dir, ignore_errors=True)
    temp_dir.mkdir(parents=True, exist_ok=True)

    segments: list[str] = []
    try:
        for index, clip in enumerate(plan.clips, start=1):
            ass_name = f"seg_{index:03d}.ass"
            out_name = f"seg_{index:03d}.mp4"
            _write_segment_subtitles(clip, style, settings, temp_dir / ass_name, font)
            run_ffmpeg(
                segment_command(settings, clip, ass_name, out_name, font.fonts_dir),
                cwd=temp_dir,
            )
            segments.append(out_name)

        concat_list = temp_dir / "segments.txt"
        concat_list.write_text(
            "".join(f"file '{name}'\n" for name in segments), encoding="utf-8"
        )

        settings.output_dir.mkdir(parents=True, exist_ok=True)
        output = next_output_path(
            settings.output_dir,
            plan.date,
            str(_setting(settings, "output_name", "DailyReel_{date}.mp4")),
        )
        run_ffmpeg(
            [
                settings.ffmpeg, "-hide_banner", "-v", "error", "-y",
                "-f", "concat", "-safe", "0", "-i", concat_list.name,
                "-c", "copy", "-movflags", "+faststart", str(output),
            ],
            cwd=temp_dir,
        )

        stats = validate_output(settings, output, plan)
    except (RenderError, EditPlanError, probe_mod.FFprobeError):
        log.error("render failed; temp files kept in %s", temp_dir)
        raise

    preview_files: list[Path] = []
    if previews:
        preview_files = make_previews(
            settings, output, stats["duration"], settings.data_dir / PREVIEW_DIRNAME
        )

    if not keep_temp:
        shutil.rmtree(temp_dir, ignore_errors=True)

    return RenderResult(
        output=output,
        plan=plan,
        font=font,
        previews=preview_files,
        temp_dir=temp_dir if keep_temp else None,
        **stats,
    )
