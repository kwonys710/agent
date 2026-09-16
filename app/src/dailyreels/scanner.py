from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from dailyreels.config import Settings
from dailyreels.models import Manifest, ScanError, VideoMeta
from dailyreels import probe as probe_mod

log = logging.getLogger(__name__)

Prober = Callable[[Path], dict[str, Any]]


def resolve_timezone(name: str):
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # tzdata missing (common on Windows) -> system local time
        log.warning("timezone %s unavailable, falling back to system local time", name)
        return None


def find_videos(settings: Settings) -> list[Path]:
    """Videos sitting directly in the root. No recursion, no subfolders."""
    found: list[Path] = []
    for entry in sorted(settings.root.iterdir(), key=lambda p: p.name.lower()):
        if entry.is_dir():
            continue
        if entry.name.startswith((".", "~$")):
            continue
        if entry.suffix.lower() not in settings.video_extensions:
            continue
        found.append(entry)
    return found


def _to_local(moment: datetime, tzinfo) -> datetime:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    local = moment.astimezone(tzinfo) if tzinfo else moment.astimezone()
    return local.replace(tzinfo=None)


def build_video_meta(
    path: Path,
    payload: dict[str, Any],
    root: Path,
    tzinfo=None,
) -> VideoMeta:
    stream = probe_mod.video_stream(payload)
    if stream is None:
        raise probe_mod.FFprobeError(f"no video stream in {path.name}")

    stat = path.stat()
    created = probe_mod.parse_creation_time(payload, stream)
    if created is not None:
        captured_at = _to_local(created, tzinfo)
        source = "creation_time"
    else:
        captured_at = _to_local(
            datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc), tzinfo
        )
        source = "file_mtime"

    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)
    rotation = probe_mod.parse_rotation(stream)

    return VideoMeta(
        file=path.name,
        captured_at=captured_at,
        captured_at_source=source,
        duration=probe_mod.parse_duration(payload, stream),
        width=width,
        height=height,
        fps=probe_mod.parse_fps(stream),
        codec=str(stream.get("codec_name") or "unknown"),
        orientation=probe_mod.orientation_of(width, height, rotation),
        rotation=rotation,
        file_size=stat.st_size,
    )


def scan(settings: Settings, prober: Prober | None = None) -> Manifest:
    """Probe every root-level video and return a manifest sorted by capture time."""
    probe_fn: Prober = prober or (
        lambda path: probe_mod.run_ffprobe(path, ffprobe=settings.ffprobe)
    )
    tzinfo = resolve_timezone(settings.timezone)

    videos: list[VideoMeta] = []
    errors: list[ScanError] = []

    for path in find_videos(settings):
        try:
            payload = probe_fn(path)
            videos.append(build_video_meta(path, payload, settings.root, tzinfo))
        except probe_mod.FFprobeError as exc:
            log.error("%s", exc)
            errors.append(ScanError(file=path.name, error=str(exc)))
        except OSError as exc:
            log.error("could not read %s: %s", path.name, exc)
            errors.append(ScanError(file=path.name, error=str(exc)))

    videos.sort(key=lambda video: (video.captured_at, video.file.lower()))

    return Manifest(
        root=str(settings.root),
        videos=videos,
        errors=errors,
        generated_at=datetime.now().astimezone().replace(microsecond=0),
    )
