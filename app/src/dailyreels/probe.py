from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any

from dailyreels.models import Orientation

log = logging.getLogger(__name__)

_CREATION_TIME_KEYS = ("creation_time", "com.apple.quicktime.creationdate")


class FFprobeError(RuntimeError):
    """ffprobe is missing, or refused to read a file."""


def run_ffprobe(path: Path, ffprobe: str = "ffprobe", timeout: int = 60) -> dict[str, Any]:
    """Read-only metadata probe. Never writes to or renames the source file."""
    command = [
        ffprobe,
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise FFprobeError(
            f"ffprobe not found ({ffprobe}). Install FFmpeg or set DAILYREELS_FFPROBE."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise FFprobeError(f"ffprobe timed out after {timeout}s on {path.name}") from exc

    stderr = completed.stderr.decode("utf-8", errors="replace").strip()
    if completed.returncode != 0:
        raise FFprobeError(f"ffprobe failed on {path.name}: {stderr or 'unknown error'}")

    try:
        return json.loads(completed.stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise FFprobeError(f"ffprobe returned invalid JSON for {path.name}: {exc}") from exc


def video_stream(payload: dict[str, Any]) -> dict[str, Any] | None:
    for stream in payload.get("streams", []):
        if stream.get("codec_type") == "video":
            return stream
    return None


def parse_fps(stream: dict[str, Any]) -> float:
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = stream.get(key)
        if not raw or raw in ("0/0", "0"):
            continue
        try:
            value = float(Fraction(raw))
        except (ZeroDivisionError, ValueError):
            continue
        if value > 0:
            return round(value, 3)
    return 0.0


def parse_rotation(stream: dict[str, Any]) -> int:
    """Rotation in degrees, normalised to 0/90/180/270."""
    raw: Any = None
    for side_data in stream.get("side_data_list", []) or []:
        if "rotation" in side_data:
            raw = side_data["rotation"]
            break
    if raw is None:
        raw = (stream.get("tags") or {}).get("rotate")
    if raw is None:
        return 0
    try:
        return int(round(float(raw))) % 360
    except (TypeError, ValueError):
        return 0


def parse_duration(payload: dict[str, Any], stream: dict[str, Any]) -> float:
    for raw in (stream.get("duration"), payload.get("format", {}).get("duration")):
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0.0


def orientation_of(width: int, height: int, rotation: int) -> Orientation:
    if width <= 0 or height <= 0:
        return "unknown"
    if rotation % 180 == 90:
        width, height = height, width
    if height > width:
        return "portrait"
    if width > height:
        return "landscape"
    return "square"


def parse_creation_time(payload: dict[str, Any], stream: dict[str, Any]) -> datetime | None:
    """ffprobe reports creation_time in UTC; return an aware datetime."""
    sources = [payload.get("format", {}).get("tags") or {}, stream.get("tags") or {}]
    for tags in sources:
        for key in _CREATION_TIME_KEYS:
            raw = tags.get(key)
            if not raw:
                continue
            text = str(raw).strip().replace("Z", "+00:00")
            try:
                parsed = datetime.fromisoformat(text)
            except ValueError:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            if parsed.year > 1970:
                return parsed
    return None
